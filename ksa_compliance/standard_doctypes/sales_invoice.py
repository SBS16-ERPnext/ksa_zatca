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
        frappe.throw(_("ZATCA Additional Fields not found for this invoice. Please ensure the invoice is submitted."))
    
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
    error_list = []
    is_phase_2_enabled_for_company = ZATCABusinessSettings.is_enabled_for_company(self.company)
    
    # Validate taxes
    if ZATCAPhase1BusinessSettings.is_enabled_for_company(self.company) or is_phase_2_enabled_for_company:
        if len(self.taxes) == 0:
            error_list.append(_('Please include tax rate in Sales Taxes and Charges Table'))

    if is_phase_2_enabled_for_company:
        settings = ZATCABusinessSettings.for_company(self.company)
        
        # Validate B2B customer
        if settings.type_of_business_transactions == 'Standard Tax Invoices':
            customer = cast(Customer, frappe.get_doc('Customer', self.customer))
            if not is_b2b_customer(customer):
                error_list.append(
                    ft(
                        'Company <b>$company</b> uses Standard Tax Invoices(B2B). '
                        'Customer <b>$customer</b> must have a VAT or one of the other IDs.',
                        company=self.company,
                        customer=self.customer,
                    )
                )
        
        # Validate customer address for non-B2C customers
        customer = cast(Customer, frappe.get_doc('Customer', self.customer))
        if not customer.get('custom_b2c'):
            # Get customer address
            address = None
            if self.customer_address:
                address = frappe.get_doc('Address', self.customer_address)
            elif customer.get('customer_primary_address'):
                address = frappe.get_doc('Address', customer.customer_primary_address)
            
            if not address:
                error_list.append(_('Customer address is mandatory for non-B2C customers as per ZATCA regulations'))
            else:
                # Validate all required address fields
                if not address.address_line1:
                    error_list.append(_('Address Line 1 is required in customer address'))
                
                if not address.get('address_line2'):
                    error_list.append(_('Address Line 2 is required in customer address'))
                
                if not address.get('custom_building_number'):
                    error_list.append(_('Building Number is required in customer address'))
                elif len(str(address.get('custom_building_number'))) != 4:
                    error_list.append(_('Building Number must be exactly 4 digits in customer address'))
                
                if not address.city:
                    error_list.append(_('City is required in customer address'))
                
                if not address.pincode:
                    error_list.append(_('Postal Code is required in customer address'))
                elif len(str(address.pincode)) != 5:
                    error_list.append(_('Postal Code must be exactly 5 digits in customer address'))
                
                if not address.get('custom_area'):
                    error_list.append(_('District is required in customer address'))
        
        # Validate items have tax category
        for item in self.items:
            if not item.get('custom_zatca_item_tax_category'):
                error_list.append(
                    _('ZATCA Tax Category is required for item: {0}').format(item.item_code)
                )

    if error_list:
        # Format all errors as a single message
        error_messages = '<br><br>'.join(f'• {msg}' for msg in error_list)
        frappe.throw(
            msg=f'<div style="text-align: left;">{error_messages}</div>',
            title=_('ZATCA Validation Errors')
        )
