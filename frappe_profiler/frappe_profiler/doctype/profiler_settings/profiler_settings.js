// Copyright (c) 2026, Frappe Profiler contributors
// For license information, please see license.txt

// Populate the `Tracked Apps` child table's `app_name` Autocomplete
// with the bench's actual installed apps — same UX as picking a
// Module Def, but for the list of apps rather than modules.
//
// Why on refresh: the Autocomplete options live on the grid docfield,
// which Frappe re-creates whenever the form rebinds. Setting options
// on refresh survives reloads and "refresh" Ctrl-S cycles.

frappe.ui.form.on("Profiler Settings", {
	refresh(frm) {
		frappe.call({
			method: "frappe_profiler.api.get_installed_apps_for_tracking",
			callback(r) {
				if (!r || !r.message) {
					return;
				}
				const apps = r.message;
				const grid = frm.fields_dict.tracked_apps.grid;
				if (!grid) {
					return;
				}
				// Autocomplete fieldtype accepts options as a
				// newline-separated string (Frappe's renderer splits
				// on \n). Passing an array also works in v14+, but
				// the string form is the safe cross-version shape.
				grid.update_docfield_property(
					"app_name",
					"options",
					apps.join("\n")
				);
			},
		});
	},
});
