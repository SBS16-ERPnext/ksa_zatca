from datetime import date
from typing import cast

import frappe
import frappe.utils.background_jobs
from erpnext.accounts.doctype.pos_invoice.pos_invoice import POSInvoice
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice
from erpnext.selling.doctype.customer.customer import Customer
from frappe import _
from result import is_ok

from ksa_compliance import logger
from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
    SalesInvoiceAdditionalFields,
    is_b2b_customer,
)
from ksa_compliance.ksa_compliance.doctype.zatca_business_settings.zatca_business_settings import ZATCABusinessSettings
from ksa_compliance.ksa_compliance.doctype.zatca_egs.zatca_egs import ZATCAEGS
from ksa_compliance.ksa_compliance.doctype.zatca_phase_1_business_settings.zatca_phase_1_business_settings import (
    ZATCAPhase1BusinessSettings,
)
from ksa_compliance.ksa_compliance.doctype.zatca_precomputed_invoice.zatca_precomputed_invoice import (
    ZATCAPrecomputedInvoice,
)

from ksa_compliance.translation import ft

IGNORED_INVOICES = set()
SKIP_VALIDATION = False


def set_skip_validation(skip: bool) -> None:
    global SKIP_VALIDATION
    SKIP_VALIDATION = skip


@frappe.whitelist()
def send_invoice_to_zatca(invoice_name: str, doctype: str = 'Sales Invoice'):
    """
    Manually send an invoice to ZATCA.
    This is used when auto_submit_to_zatca is disabled.
    """
    if not frappe.has_permission(doctype, 'write', invoice_name):
        frappe.throw(_("Not permitted to send this invoice to ZATCA"))
    
    # Get the Sales Invoice Additional Fields document
    filters = {
        'invoice_doctype': doctype,
        'sales_invoice': invoice_name,
        'is_latest': 1
    }
    
    additional_fields = frappe.get_all(
        'Sales Invoice Additional Fields',
        filters=filters,
        fields=['name', 'docstatus', 'integration_status'],
        order_by='creation desc',
        limit=1
    )
    
    if not additional_fields:
        # Create additional fields if not found
        logger.info(f"Creating ZATCA Additional Fields for {invoice_name} as it was not found")
        invoice_doc = frappe.get_doc(doctype, invoice_name)
        
        # Validate invoice is submitted
        if invoice_doc.docstatus != 1:
            frappe.throw(_("Invoice must be submitted before sending to ZATCA"))
        
        # Create additional fields using the same logic as normal flow
        si_additional_fields_doc = SalesInvoiceAdditionalFields.create_for_invoice(invoice_name, doctype)
        precomputed_invoice = ZATCAPrecomputedInvoice.for_invoice(invoice_name)
        
        if precomputed_invoice:
            logger.info(f'Using precomputed invoice {precomputed_invoice.name} for {invoice_name}')
            si_additional_fields_doc.use_precomputed_invoice(precomputed_invoice)
        
        si_additional_fields_doc.insert()
        additional_field_doc = si_additional_fields_doc
    else:
        additional_field_doc = frappe.get_doc('Sales Invoice Additional Fields', additional_fields[0].name)
    
    # Check if already submitted successfully
    if additional_field_doc.integration_status in ['Accepted', 'Accepted with warnings']:
        status_msg = _("This invoice has already been accepted by ZATCA")
        if additional_field_doc.integration_status == 'Accepted with warnings':
            status_msg = _("This invoice has already been accepted by ZATCA with warnings")
        frappe.msgprint(status_msg, indicator='blue')
        return {'status': 'already_submitted', 'message': status_msg}
    logger.info(f'Manually submitting {additional_field_doc.name} to ZATCA')
    result = additional_field_doc.submit_to_zatca()
    
    if is_ok(result):
        message = result.ok_value
        logger.info(f'Manual submission successful: {message}')
        frappe.msgprint(_(f"Successfully submitted to ZATCA: {message}"), indicator='green')
        return {'status': 'success', 'message': message}
    else:
        error = result.err_value
        logger.error(f'Manual submission failed: {error}')
        frappe.msgprint(_(f"Failed to submit to ZATCA: {error}"), indicator='red')
        return {'status': 'error', 'message': error}


def ignore_additional_fields_for_invoice(name: str) -> None:
    global IGNORED_INVOICES
    IGNORED_INVOICES.add(name)


def clear_additional_fields_ignore_list() -> None:
    global IGNORED_INVOICES
    IGNORED_INVOICES.clear()


def create_sales_invoice_additional_fields_doctype(self: SalesInvoice | POSInvoice, method):
    if self.doctype == 'Sales Invoice' and not _should_enable_zatca_for_invoice(self.name):
        logger.info(f"Skipping additional fields for {self.name} because it's before start date")
        return

    settings = ZATCABusinessSettings.for_invoice(self.name, self.doctype)
    if not settings:
        if ZATCABusinessSettings.is_revoked_for_company(self.company):
            logger.info(f'Skipping additional fields for {self.name} because of revoked ZATCA settings')
            return
        logger.info(f'Skipping additional fields for {self.name} because of missing ZATCA settings')
        return

    if not settings.enable_zatca_integration:
        logger.info(f'Skipping additional fields for {self.name} because ZATCA integration is disabled in settings')
        return

    global IGNORED_INVOICES
    if self.name in IGNORED_INVOICES:
        logger.info(f"Skipping additional fields for {self.name} because it's in the ignore list")
        return

    if self.doctype == 'Sales Invoice' and self.is_consolidated:
        logger.info(f"Skipping additional fields for {self.name} because it's consolidated")
        return

    si_additional_fields_doc = SalesInvoiceAdditionalFields.create_for_invoice(self.name, self.doctype)
    precomputed_invoice = ZATCAPrecomputedInvoice.for_invoice(self.name)
    is_live_sync = settings.is_live_sync
    if precomputed_invoice:
        logger.info(f'Using precomputed invoice {precomputed_invoice.name} for {self.name}')
        si_additional_fields_doc.use_precomputed_invoice(precomputed_invoice)

        egs_settings = ZATCAEGS.for_device(precomputed_invoice.device_id)
        if not egs_settings:
            logger.warning(f'Could not find EGS for device {precomputed_invoice.device_id}')
        else:
            # EGS Setting overrides company-wide setting
            is_live_sync = egs_settings.is_live_sync

    si_additional_fields_doc.insert()
    
    # Check if auto submit is enabled
    auto_submit = settings.auto_submit_to_zatca if hasattr(settings, 'auto_submit_to_zatca') else True
    
    if is_live_sync and auto_submit:
        # We're running in the context of invoice submission (on_submit hook). We only want to run our ZATCA logic if
        # the invoice submits successfully after on_submit is run successfully from all apps.
        frappe.utils.background_jobs.enqueue(
            _submit_additional_fields, doc=si_additional_fields_doc, enqueue_after_commit=True
        )


def _submit_additional_fields(doc: SalesInvoiceAdditionalFields):
    logger.info(f'Submitting {doc.name}')
    result = doc.submit_to_zatca()
    message = result.ok_value if is_ok(result) else result.err_value
    logger.info(f'Submission result: {message}')


def _should_enable_zatca_for_invoice(invoice_id: str) -> bool:
    start_date = date(2024, 3, 1)

    if frappe.db.table_exists('Vehicle Booking Item Info'):
        # noinspection SqlResolve
        records = frappe.db.sql(
            'SELECT bv.local_trx_date_time FROM `tabVehicle Booking Item Info` bvii '
            'JOIN `tabBooking Vehicle` bv ON bvii.parent = bv.name WHERE bvii.sales_invoice = %(invoice)s',
            {'invoice': invoice_id},
            as_dict=True,
        )
        if records:
            local_date = records[0]['local_trx_date_time'].date()
            return local_date >= start_date

    posting_date = frappe.db.get_value('Sales Invoice', invoice_id, 'posting_date')
    return posting_date >= start_date


def prevent_cancellation_of_sales_invoice(self: SalesInvoice | POSInvoice, method) -> None:
    is_phase_2_enabled_for_company = ZATCABusinessSettings.is_enabled_for_company(self.company)
    if is_phase_2_enabled_for_company:
        frappe.throw(
            msg=_('You cannot cancel sales invoice according to ZATCA Regulations.'),
            title=_('This Action Is Not Allowed'),
        )


def validate_sales_invoice(self: SalesInvoice | POSInvoice, method) -> None:
    if SKIP_VALIDATION:
        return

    error_list = []
    is_phase_2_enabled_for_company = ZATCABusinessSettings.is_enabled_for_company(self.company)
    
    # Validate taxes
    if ZATCAPhase1BusinessSettings.is_enabled_for_company(self.company) or is_phase_2_enabled_for_company:
        if len(self.taxes) == 0:
            error_list.append(_('Please include tax rate in Sales Taxes and Charges Table'))

    if is_phase_2_enabled_for_company:
        settings = ZATCABusinessSettings.for_company(self.company)
        customer = cast(Customer, frappe.get_doc('Customer', self.customer))
        
        # BR-KSA-37: Validate seller address building number (ALWAYS REQUIRED)
        _validate_seller_building_number(settings, error_list)
        
        # Get buyer identifiers
        buyer_vat = (customer.custom_vat_registration_number or '').strip()
        additional_ids = customer.get('custom_additional_ids') or []
        crn_value, nat_value = _get_buyer_crn_and_nat(additional_ids)
        
        # Determine if this is a B2B customer
        is_b2b = is_b2b_customer(customer)
        is_standard_invoice = settings.type_of_business_transactions == 'Standard Tax Invoices'
        
        # Validate B2B requirement for Standard Tax Invoices
        if is_standard_invoice and not is_b2b:
            error_list.append(
                ft(
                    'Company <b>$company</b> uses Standard Tax Invoices (B2B). '
                    'Customer <b>$customer</b> must have a VAT Number.',
                    company=self.company,
                    customer=self.customer,
                )
            )
        
        # For B2B customers with Standard Tax Invoices: VAT, CRN, and NAT are MANDATORY
        if is_standard_invoice and is_b2b:
            if not buyer_vat:
                error_list.append(_('B2B Customer VAT Registration Number is mandatory for Standard Tax Invoices'))
            
            if not crn_value:
                error_list.append(_('B2B Customer CRN (Commercial Registration Number) is mandatory for Standard Tax Invoices'))
            
            # if not nat_value:
            #     error_list.append(_('B2B Customer NAT (National ID) is mandatory for Standard Tax Invoices'))
        
        # Validate formats if values exist
        _validate_buyer_vat_format(buyer_vat, error_list)
        _validate_buyer_crn_format(crn_value, error_list)
        # _validate_buyer_nat_format(nat_value, error_list)
        
        # BR-KSA-56: Credit/Debit note validation
        if self.is_return and not self.custom_return_against_additional_references:
            error_list.append(
                _('[BR-KSA-56] For credit/debit notes, the billing reference ID (return against) is mandatory')
            )
        
        # Validate customer address (ALWAYS REQUIRED)
        _validate_customer_address(self, customer, error_list)
        
        # Validate items have tax category
        _validate_item_tax_categories(self.items, error_list)

    if error_list:
        # Format all errors as a single message
        error_messages = '<br><br>'.join(f'• {msg}' for msg in error_list)
        frappe.throw(
            msg=f'<div style="text-align: left;">{error_messages}</div>',
            title=_('ZATCA Validation Errors')
        )


# Helper functions for validation
def _validate_seller_building_number(settings: ZATCABusinessSettings, error_list: list) -> None:
    """BR-KSA-37: Validate seller address building number (must be 4 digits)"""
    if not settings.building_number:
        error_list.append(_('[BR-KSA-37] Seller address building number is required'))
    else:
        building_num = str(settings.building_number).strip()
        if len(building_num) != 4:
            error_list.append(
                _('[BR-KSA-37] Seller address building number must be exactly 4 digits. Current: "{0}" ({1} digits)').format(
                    building_num, len(building_num)
                )
            )
        elif not building_num.isdigit():
            error_list.append(
                _('[BR-KSA-37] Seller address building number must contain only digits. Current: "{0}"').format(building_num)
            )



def _get_buyer_crn_and_nat(additional_ids: list) -> tuple:
    """Extract CRN and NAT values from buyer's additional IDs"""
    crn_value = None
    nat_value = None
    
    for buyer_id in additional_ids:
        if buyer_id.get('type_code') == 'CRN' and buyer_id.get('value'):
            crn_value = buyer_id.get('value').strip()
        if buyer_id.get('type_code') == 'NAT' and buyer_id.get('value'):
            nat_value = buyer_id.get('value').strip()
    
    return crn_value, nat_value


def _validate_buyer_vat_format(buyer_vat: str, error_list: list) -> None:
    """BR-KSA-44: Validate buyer VAT format (15 digits, starts and ends with "3")"""
    if not buyer_vat:
        return
    
    buyer_vat_clean = buyer_vat.replace(' ', '').replace('-', '')
    if len(buyer_vat_clean) != 15:
        error_list.append(
            _('[BR-KSA-44] Buyer VAT registration number must be exactly 15 digits. Current: {0} digits').format(
                len(buyer_vat_clean)
            )
        )
    elif not buyer_vat_clean.isdigit():
        error_list.append(_('[BR-KSA-44] Buyer VAT registration number must contain only digits'))
    elif buyer_vat_clean[0] != '3' or buyer_vat_clean[-1] != '3':
        error_list.append(
            _('[BR-KSA-44] Buyer VAT registration number must start with "3" and end with "3". Current: starts with "{0}", ends with "{1}"').format(
                buyer_vat_clean[0], buyer_vat_clean[-1]
            )
        )


def _validate_buyer_crn_format(crn_value: str, error_list: list) -> None:
    """BR-KSA-F-08: Validate buyer CRN format (10 digits)"""
    if not crn_value:
        return
    
    if len(crn_value) != 10:
        error_list.append(
            _('[BR-KSA-F-08] Buyer CRN (Commercial Registration Number) must be exactly 10 digits. Current: "{0}" ({1} digits)').format(
                crn_value, len(crn_value)
            )
        )
    elif not crn_value.isdigit():
        error_list.append(
            _('[BR-KSA-F-08] Buyer CRN must contain only digits. Current: "{0}"').format(crn_value)
        )


def _validate_buyer_nat_format(nat_value: str, error_list: list) -> None:
    """Validate buyer National ID format (10 digits)"""
    if not nat_value:
        return
    
    if len(nat_value) != 10:
        error_list.append(
            _('Buyer National ID (NAT) must be exactly 10 digits. Current: "{0}" ({1} digits)').format(
                nat_value, len(nat_value)
            )
        )
    elif not nat_value.isdigit():
        error_list.append(
            _('Buyer National ID must contain only digits. Current: "{0}"').format(nat_value)
        )


def _validate_customer_address(invoice: SalesInvoice | POSInvoice, customer: Customer, error_list: list) -> None:
    """Validate customer address and all required fields"""
    # Get customer address
    address = None
    if invoice.customer_address:
        address = frappe.get_doc('Address', invoice.customer_address)
    elif customer.get('customer_primary_address'):
        address = frappe.get_doc('Address', customer.customer_primary_address)
    
    if not address:
        error_list.append(_('Customer address is mandatory as per ZATCA regulations'))
        return
    
    # Validate required address fields
    if not address.address_line1:
        error_list.append(_('Address Line 1 (Street Name) is required in customer address'))
    
    if not address.get('address_line2'):
        error_list.append(_('Address Line 2 (Additional Street) is required in customer address'))
    
    # BR-KSA-37: Building number validation (4 digits)
    if not address.get('custom_building_number'):
        error_list.append(_('[BR-KSA-37] Buyer address building number is required'))
    else:
        buyer_building = str(address.get('custom_building_number')).strip()
        if len(buyer_building) != 4:
            error_list.append(
                _('[BR-KSA-37] Buyer address building number must be exactly 4 digits. Current: "{0}" ({1} digits)').format(
                    buyer_building, len(buyer_building)
                )
            )
        elif not buyer_building.isdigit():
            error_list.append(
                _('[BR-KSA-37] Buyer address building number must contain only digits. Current: "{0}"').format(buyer_building)
            )
    
    if not address.city:
        error_list.append(_('City is required in customer address'))
    
    # Postal code validation (5 digits)
    if not address.pincode:
        error_list.append(_('Postal Code is required in customer address'))
    else:
        postal_code = str(address.pincode).strip()
        if len(postal_code) != 5:
            error_list.append(
                _('Postal Code must be exactly 5 digits. Current: "{0}" ({1} digits)').format(
                    postal_code, len(postal_code)
                )
            )
        elif not postal_code.isdigit():
            error_list.append(
                _('Postal Code must contain only digits. Current: "{0}"').format(postal_code)
            )
    
    if not address.get('custom_area'):
        error_list.append(_('District is required in customer address'))


def _validate_item_tax_categories(items: list, error_list: list) -> None:
    """Validate that all items have ZATCA tax category"""
    for item in items:
        if not item.get('custom_zatca_item_tax_category'):
            error_list.append(
                _('ZATCA Tax Category is required for item: {0}').format(item.item_code)
            )
