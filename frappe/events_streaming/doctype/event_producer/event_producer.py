# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import json
import requests
from frappe import _
from frappe.model.document import Document
from frappe.frappeclient import FrappeClient
from frappe.utils.background_jobs import get_jobs
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

class EventProducer(Document):
	def before_insert(self):
		self.create_event_consumer()

	def on_update(self):
		self.update_event_consumer()
		self.create_custom_fields()

	def create_event_consumer(self):
		'''register event consumer on the producer site'''
		producer_site = FrappeClient(self.producer_url, verify=False)
		subscribed_doctypes = []
		for entry in self.event_configuration:
			if entry.has_mapping:
				#if it has mapping then on event consumer's site it should subscribe to remote doctype
				subscribed_doctypes.append(frappe.db.get_value('Document Type Mapping', entry.mapping, 'remote_doctype'))
			else:
				subscribed_doctypes.append(entry.ref_doctype)
		response = producer_site.post_request({
			'cmd': 'frappe.events_streaming.doctype.event_consumer.event_consumer.register_consumer',
			'event_consumer': get_current_node(),
			'subscribed_doctypes': json.dumps(subscribed_doctypes),
			'user': self.user
		})
		response = json.loads(response)
		self.api_key = response['api_key']
		self.api_secret =  response['api_secret']
		self.last_update = response['last_update']

	def create_custom_fields(self):
		'''create custom field to store remote docname and remote site url'''
		for entry in self.event_configuration:
			if not entry.use_same_name:
				if not frappe.db.exists('Custom Field', {'fieldname': 'remote_docname', 'dt': entry.ref_doctype}):
					df = dict(fieldname='remote_docname', label='Remote Document Name', fieldtype='Data', read_only=1, print_hide=1)
					create_custom_field(entry.ref_doctype, df)
				if not frappe.db.exists('Custom Field', {'fieldname': 'remote_site_name', 'dt': entry.ref_doctype}):
					df = dict(fieldname='remote_site_name', label='Remote Site', fieldtype='Data', read_only=1, print_hide=1)
					create_custom_field(entry.ref_doctype, df)

	def update_event_consumer(self):
		producer_site = get_producer_site(self.producer_url)
		event_consumer = producer_site.get_doc('Event Consumer', get_current_node())
		if event_consumer:
			event_consumer.subscribed_doctypes = []
			for entry in self.event_configuration:
				if entry.has_mapping:
					#if it has mapping then on event consumer's site it should subscribe to remote doctype
					event_consumer.subscribed_doctypes.append({
						'ref_doctype': frappe.db.get_value('Document Type Mapping', entry.mapping, 'remote_doctype')
					})
				else:
					event_consumer.subscribed_doctypes.append({
						'ref_doctype': entry.ref_doctype
					})
			event_consumer.user = self.user
			producer_site.update(event_consumer)

def get_current_node():
	current_node = frappe.utils.get_url()
	parts = current_node.split(':')
	if not len(parts) > 2:
		port = frappe.conf.http_port or frappe.conf.webserver_port
		current_node += ':' + str(port)
	return current_node

def get_producer_site(producer_url):
	producer_doc = frappe.get_doc('Event Producer', producer_url)
	producer_site = FrappeClient(
		url=producer_url,
		api_key=producer_doc.api_key,
		api_secret=producer_doc.get_password('api_secret'),
		frappe_authorization_source='Event Consumer'
	)
	return producer_site

@frappe.whitelist()
def pull_producer_data():
	response = requests.get(get_current_node())
	if response.status_code == 200:
		'''Fetch data from producer node.'''
		for event_producer in frappe.get_all('Event Producer'):
			pull_from_node(event_producer.name)
		return 'success'

@frappe.whitelist()
def pull_from_node(event_producer):
	event_producer = frappe.get_doc('Event Producer', event_producer)
	producer_site = get_producer_site(event_producer.producer_url)
	last_update = event_producer.last_update

	(doctypes, mapping_config, naming_config) = get_config(event_producer.event_configuration)

	updates = get_updates(producer_site, last_update, doctypes)

	for update in updates:
		update.use_same_name = naming_config.get(update.ref_doctype)
		mapping = mapping_config.get(update.ref_doctype)
		if mapping:
			update.mapping = mapping
			update = get_mapped_update(update)
		if not update.update_type == 'Delete':
			update.data = json.loads(update.data)

		sync(update, producer_site, event_producer)

def get_config(event_config):
	doctypes, mapping_config, naming_config = [], {}, {}

	for entry in event_config:
		if entry.has_mapping:
			(mapped_doctype, mapping) = frappe.db.get_value('Document Type Mapping', entry.mapping, ['remote_doctype', 'name'])
			mapping_config[mapped_doctype] = mapping
			naming_config[mapped_doctype] = entry.use_same_name
			doctypes.append(mapped_doctype)
		else:
			naming_config[entry.ref_doctype] = entry.use_same_name
			doctypes.append(entry.ref_doctype)
	return (doctypes, mapping_config, naming_config)

def sync(update, producer_site, event_producer, in_retry=False):
	try:
		if update.update_type == 'Create':
			set_insert(update, producer_site, event_producer.name)
		if update.update_type == 'Update':
			set_update(update, producer_site)
		if update.update_type == 'Delete':
			set_delete(update)
		if in_retry:
			return 'Synced'
		log_event_sync(update, event_producer.name, 'Synced')

	except Exception:
		if in_retry:
			return 'Failed'
		log_event_sync(update, event_producer.name, 'Failed', frappe.get_traceback())

	frappe.db.set_value('Event Producer', event_producer.name, 'last_update', update.name)
	frappe.db.commit()

def set_insert(update, producer_site, event_producer):
	if frappe.db.get_value(update.ref_doctype, update.docname):
		# doc already created
		return
	else:
		doc = frappe.get_doc(update.data)
		check_doc_has_dependencies(doc, producer_site)
		if update.use_same_name:
			doc.insert(set_name=update.docname)
		else:
			#if event consumer is not saving documents with the same name as the producer
			#store the remote docname in a custom field for future updates
			local_doc = doc.insert()
			set_custom_fields(local_doc, update.docname, event_producer)

def set_update(update, producer_site):
	local_doc = get_local_doc(update)
	if local_doc:
		update.data.pop('name')
		check_doc_has_dependencies(local_doc, producer_site)
		local_doc.update(update.data)
		local_doc.db_update_all()

def set_delete(update):
	local_doc = get_local_doc(update)
	if local_doc:
		local_doc.delete()

def get_updates(producer_site, last_update, doctypes):
	last_update = producer_site.get_value('Update Log', 'creation', {'name': last_update})
	filters = {'ref_doctype': ('in', doctypes)}
	if last_update:
		last_update_timestamp = last_update.get('creation')
		filters.update({'creation': ('>', last_update_timestamp)})
	docs = producer_site.get_list(
		doctype = 'Update Log',
		filters = filters,
		fields = ['update_type', 'ref_doctype', 'docname', 'data', 'name']
	)
	docs.reverse()
	return [frappe._dict(d) for d in docs]

def get_local_doc(update):
	try:
		if not update.use_same_name:
			return frappe.get_doc(update.ref_doctype, {'remote_docname': update.docname})
		else:
			return frappe.get_doc(update.ref_doctype, update.docname)
	except frappe.DoesNotExistError:
		return

def check_doc_has_dependencies(doc, producer_site):
	'''Sync child table link fields first,
	then sync link fields,
	then dynamic links'''

	meta = frappe.get_meta(doc.doctype)
	table_fields = meta.get_table_fields()
	link_fields = meta.get_link_fields()
	dl_fields = meta.get_dynamic_link_fields()
	if table_fields:
		sync_child_table_dependencies(doc, table_fields, producer_site)
	if link_fields:
		sync_link_dependencies(doc, link_fields, producer_site)
	if dl_fields:
		sync_dynamic_link_dependencies(doc, dl_fields, producer_site)
			
def sync_child_table_dependencies(doc, table_fields, producer_site):
	for df in table_fields:
		child_table = doc.get(df.fieldname)
		for entry in child_table:
			set_dependencies(entry, frappe.get_meta(entry.doctype).get_link_fields(), producer_site)
		
def sync_link_dependencies(doc, link_fields, producer_site):
	set_dependencies(doc, link_fields, producer_site)

def sync_dynamic_link_dependencies(doc, dl_fields, producer_site):
	for df in dl_fields:
		docname = doc.get(df.fieldname)
		linked_doctype = doc.get(df.options)
		if docname and not check_dependency_fulfilled(linked_doctype, docname):
			master_doc = producer_site.get_doc(linked_doctype, docname)
			frappe.get_doc(master_doc).insert(set_name=docname)
			frappe.db.commit()

def set_dependencies(doc, link_fields, producer_site):
	for df in link_fields:
		docname = doc.get(df.fieldname)
		linked_doctype = df.get_link_doctype()
		if docname and not check_dependency_fulfilled(linked_doctype, docname):
			master_doc = producer_site.get_doc(linked_doctype, docname)
			try:
				doc = frappe.get_doc(master_doc)
				doc.insert(set_name=docname)
				frappe.db.commit()
			
			#for dependency inside a dependency
			except Exception:
				check_doc_has_dependencies(frappe.get_doc(master_doc), producer_site)

def check_dependency_fulfilled(linked_doctype, docname):
	return frappe.db.exists(linked_doctype, docname)

def log_event_sync(update, event_producer, sync_status, error=None):
	doc = frappe.new_doc('Event Sync Log')
	doc.update_type = update.update_type
	doc.ref_doctype = update.ref_doctype
	doc.status = sync_status
	doc.event_producer = event_producer
	doc.producer_doc = update.docname
	doc.data = frappe.as_json(update.data)
	doc.use_same_name = update.use_same_name
	doc.mapping = update.mapping if update.mapping else None
	if update.use_same_name:
		doc.docname = update.docname
	else:
		doc.docname = frappe.db.get_value(update.ref_doctype, {'remote_docname': update.docname}, 'name')
	if error:
		doc.error = error
	doc.insert()

def get_mapped_update(update):
	mapping = frappe.get_doc('Document Type Mapping', update.mapping)
	if update.update_type != 'Delete':
		update.data = mapping.get_mapped_doc(update.data)
	update.ref_doctype = mapping.local_doctype
	return update

@frappe.whitelist()
def new_event_notification(producer_url):
	'''Pull data from producer when notified'''
	enqueued_method = 'frappe.events_streaming.doctype.event_producer.event_producer.pull_from_node'
	jobs = get_jobs()
	if not jobs or enqueued_method not in jobs[frappe.local.site]:
		frappe.enqueue(enqueued_method, queue = 'default', **{'event_producer': producer_url})

@frappe.whitelist()
def resync(update):
	update = frappe._dict(json.loads(update))
	if update.mapping:
		update = get_mapped_update(update)
		update.data = json.loads(update.data)
	producer_site = get_producer_site(update.event_producer)
	event_producer = frappe.get_doc('Event Producer', update.event_producer)
	return sync(update, producer_site, event_producer, in_retry=True)

def set_custom_fields(local_doc, remote_docname, remote_site_name):
	'''sets custom field in doc for storing remote docname'''
	frappe.db.set_value(local_doc.doctype, local_doc.name, 'remote_docname', remote_docname)
	frappe.db.set_value(local_doc.doctype, local_doc.name, 'remote_site_name', remote_site_name)