frappe.ui.form.on('Sales Invoice', {
    setup: function (frm) {
        frm.set_query('custom_return_against_additional_references', function (doc) {
            // Similar to logic in erpnext/public/js/controllers/transaction.js for return_against
            let filters = {
                'docstatus': 1,
                'is_return': 0,
                'company': doc.company
            };
            if (frm.fields_dict['customer'] && doc.customer) filters['customer'] = doc.customer;
            if (frm.fields_dict['supplier'] && doc.supplier) filters['supplier'] = doc.supplier;

            return {
                filters: filters
            };
        });
    },
    async refresh(frm) {
        await set_zatca_integration_status(frm)
        await set_zatca_discount_reason(frm)
        await add_send_to_zatca_button(frm)
    },
})



async function set_zatca_discount_reason(frm) {
    const zatca_discount_reasons = await get_zatca_discount_reason_codes()
    frm.fields_dict.custom_zatca_discount_reason.set_data(zatca_discount_reasons)
}

async function set_zatca_integration_status(frm) {
    const res = await frappe.call({
        method: "ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields.get_zatca_integration_status",
        args: {
            invoice_id: frm.doc.name,
            doctype: frm.doc.doctype
        },
    });

    const status = res.integration_status;
    if (status) {
        let color = "blue"
        if (status === 'Accepted') {
            color = "green"
        } else if (["Rejected", "Resend"].includes(status)) {
            color = "red"
        }
        var link = `/app/sales-invoice-additional-fields/${res.zatca_invoice_docname}`;
        frm.set_intro(`Zatca: <span style='color: ${color};'>${status}</span> <a href='${link}' target='_blank' style='font-weight: bold; text-decoration: none;'>(${res.zatca_invoice_docname})</a>`, color)

    }
}

async function get_zatca_discount_reason_codes() {
    const res = await frappe.call({
        method: "ksa_compliance.invoice.get_zatca_invoice_discount_reason_list"
    })
    return res.message
}

async function add_send_to_zatca_button(frm) {
    // Only show button if invoice is submitted
    if (frm.doc.docstatus !== 1) {
        return;
    }

    // Check if ZATCA is enabled and get auto_submit setting
    const settings = await frappe.call({
        method: "frappe.client.get_list",
        args: {
            doctype: "ZATCA Business Settings",
            filters: {
                company: frm.doc.company,
                enable_zatca_integration: 1
            },
            fields: ["name", "auto_submit_to_zatca", "sync_with_zatca"]
        }
    });

    if (!settings.message || settings.message.length === 0) {
        return;
    }

    const zatca_settings = settings.message[0];
    const show_button = !zatca_settings.auto_submit_to_zatca || zatca_settings.sync_with_zatca === 'Live';
    
    if (show_button) {
        frm.add_custom_button(__('Send to ZATCA'), function() {
            frappe.confirm(
                __('Are you sure you want to send this invoice to ZATCA?'),
                function() {
                    frappe.call({
                        method: "ksa_compliance.standard_doctypes.sales_invoice.send_invoice_to_zatca",
                        args: {
                            invoice_name: frm.doc.name,
                            doctype: frm.doc.doctype
                        },
                        callback: function(r) {
                            if (r.message && r.message.status === 'success') {
                                frappe.show_alert({
                                    message: __('Invoice sent to ZATCA successfully'),
                                    indicator: 'green'
                                });
                                frm.reload_doc();
                            } else if (r.message && r.message.status === 'already_submitted') {
                                frappe.show_alert({
                                    message: r.message.message || __('Invoice already submitted to ZATCA'),
                                    indicator: 'blue'
                                });
                            } else if (r.message && r.message.status === 'error') {
                                frappe.show_alert({
                                    message: r.message.message || __('Failed to send invoice to ZATCA'),
                                    indicator: 'red'
                                });
                            }
                        },
                        freeze: true,
                        freeze_message: __('Sending to ZATCA...')
                    });
                }
            );
        }, __('Actions'));
    }
}