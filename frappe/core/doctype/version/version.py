# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

# License: MIT. See LICENSE

import frappe, json

from frappe.model.document import Document
from frappe.model import no_value_fields, table_fields

class Version(Document):
	def set_diff(self, old, new):
		'''Set the data property with the diff of the docs if present'''
		diff = get_diff(old, new)
		if diff:
			self.ref_doctype = new.doctype
			self.docname = new.name
			self.data = frappe.as_json(diff)
			return True
		else:
			return False

	def for_insert(self, doc):
		updater_reference = doc.flags.updater_reference
		data = {
			'creation': doc.creation,
			'updater_reference': updater_reference,
			'created_by': doc.owner
		}
		self.ref_doctype = doc.doctype
		self.docname = doc.name
		self.data = frappe.as_json(data)

	def get_data(self):
		return json.loads(self.data)


def get_diff(old, new, for_child=False):
	'''Get diff between 2 document objects

	If there is a change, then returns a dict like:

		{
			"changed"    : [[fieldname1, old, new], [fieldname2, old, new]],
			"added"      : [[table_fieldname1, {dict}], ],
			"removed"    : [[table_fieldname1, {dict}], ],
			"row_changed": [[table_fieldname1, row_name1, row_index,
				[[child_fieldname1, old, new],
				[child_fieldname2, old, new]], ]
			],

		}'''
	if not new:
		return None

	blacklisted_fields = ["Markdown Editor", "Text Editor", "Code", "HTML Editor"]

	# capture data import if set
	data_import = new.flags.via_data_import
	updater_reference = new.flags.updater_reference

	out = frappe._dict(changed = [], added = [], removed = [],
		row_changed = [], data_import=data_import, updater_reference=updater_reference)

	for df in new.meta.fields:
		if df.fieldtype in no_value_fields and df.fieldtype not in table_fields:
			continue

		old_value, new_value = old.get(df.fieldname), new.get(df.fieldname)

		if df.fieldtype in table_fields:
			# make maps
			old_row_by_name, new_row_by_name = {}, {}
			for d in old_value:
				old_row_by_name[d.name] = d
			for d in new_value:
				new_row_by_name[d.name] = d

			# check rows for additions, changes
			for i, d in enumerate(new_value):
				if d.name in old_row_by_name:
					diff = get_diff(old_row_by_name[d.name], d, for_child=True)
					if diff and diff.changed:
						out.row_changed.append((df.fieldname, i, d.name, diff.changed))
				else:
					out.added.append([df.fieldname, d.as_dict()])

			# check for deletions
			for d in old_value:
				if not d.name in new_row_by_name:
					out.removed.append([df.fieldname, d.as_dict()])

		elif (old_value != new_value):
			if df.fieldtype not in blacklisted_fields:
				old_value = old.get_formatted(df.fieldname) if old_value else old_value
				new_value = new.get_formatted(df.fieldname) if new_value else new_value

			if old_value != new_value:
				out.changed.append((df.fieldname, old_value, new_value))

	# docstatus
	if not for_child and old.docstatus != new.docstatus:
		out.changed.append(['docstatus', old.docstatus, new.docstatus])

	if any((out.changed, out.added, out.removed, out.row_changed)):
		return out

	else:
		return None

def on_doctype_update():
	frappe.db.add_index("Version", ["ref_doctype", "docname"])
