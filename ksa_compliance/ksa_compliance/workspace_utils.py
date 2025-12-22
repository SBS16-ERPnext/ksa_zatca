import frappe

def apply_zatca_workspace_visibility(doc=None, method=None, login_manager=None):
        from liveerp_app.sbs_managers.sbs_config_manager import sbs_config_manager
        show_zatca = sbs_config_manager.get_boolean("ShowZATCA", False)
        ws_name = "ZATCA"
        desired_value = 0 if show_zatca else 1
        current_value = frappe.db.get_value("Workspace", ws_name, "is_hidden")
        if current_value != desired_value:
            frappe.db.set_value("Workspace", ws_name, "is_hidden", desired_value)
            frappe.db.commit()