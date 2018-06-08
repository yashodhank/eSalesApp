from __future__ import unicode_literals
import frappe
import json
import hashlib
import time
import datetime
import os
import file_manager
from file_manager import upload
from base import validate_method
from erpnext.hr.doctype.leave_block_list.leave_block_list import get_applicable_block_dates
from erpnext.hr.doctype.leave_application.leave_application import get_number_of_leave_days, is_lwp, get_leave_balance_on
from frappe.utils import cint, date_diff, flt, getdate, formatdate, get_fullname


#VALIDATION METHOD
def validate_dates_acorss_allocation(employee, leave_type, from_date, to_date):
	def _get_leave_alloction_record(date):
		allocation = frappe.db.sql("""select name from `tabLeave Allocation`
			where employee=%s and leave_type=%s and docstatus=1
			and %s between from_date and to_date""", (employee, leave_type, date))

		return allocation and allocation[0][0]

	allocation_based_on_from_date = _get_leave_alloction_record(from_date)
	allocation_based_on_to_date = _get_leave_alloction_record(to_date)

	if not (allocation_based_on_from_date or allocation_based_on_to_date):
		return "Application period cannot be outside leave allocation period"

	elif allocation_based_on_from_date != allocation_based_on_to_date:
		return "Application period cannot be across two alocation records"
	return ""

def validate_back_dated_application(employee, leave_type, to_date):
	future_allocation = frappe.db.sql("""select name, from_date from `tabLeave Allocation` where employee=%s and leave_type=%s and docstatus=1 and from_date > %s and carry_forward=1""", (employee, leave_type, to_date), as_dict=1)

	if future_allocation:
		return "Leave cannot be applied/cancelled before {0}, as leave balance has already been carry-forwarded in the future leave allocation record {1}".format(formatdate(future_allocation[0].from_date), future_allocation[0].name)
	return ""

def validate_balance_leaves(employee, leave_type, from_date, to_date, half_day, half_day_date, status):
	total_leave_days = 0
	if from_date and to_date:
		total_leave_days = get_number_of_leave_days(employee, leave_type, from_date, to_date, half_day, half_day_date)

		if total_leave_days == 0:
			return "The day(s) on which you are applying for leave are holidays. You need not apply for leave."

		if not is_lwp(leave_type):
			leave_balance = get_leave_balance_on(employee, leave_type, from_date, consider_all_leaves_in_the_allocation_period=True)

			if status != "Rejected" and leave_balance < total_leave_days:
				if frappe.db.get_value("Leave Type", leave_type, "allow_negative"):
					return "Note: There is not enough leave balance for Leave Type {0}".format(leave_type)
				else:
					return "There is not enough leave balance for Leave Type {0}".format(leave_type)
	return total_leave_days

def validate_leave_overlap(total_leave_days, employee, from_date, to_date, half_day, half_day_date):
	def _get_total_leaves_on_half_day(employee, half_day_date, name):
		leave_count_on_half_day_date = frappe.db.sql("""select count(name) from `tabLeave Application`
			where employee = %(employee)s
			and docstatus < 2
			and status in ("Open", "Approved")
			and half_day = 1
			and half_day_date = %(half_day_date)s
			and name != %(name)s""", {
				"employee": employee,
				"half_day_date": half_day_date,
				"name": name
			})[0][0]

		return leave_count_on_half_day_date * 0.5

	def _throw_overlap_error(employee, d):
		return "Employee {0} has already applied for {1} between {2} and {3}".format(employee, d['leave_type'], formatdate(d['from_date']), formatdate(d['to_date']))

	name = "New Leave Application"

	for d in frappe.db.sql("""
		select
			name, leave_type, posting_date, from_date, to_date, total_leave_days, half_day_date
		from `tabLeave Application`
		where employee = %(employee)s and docstatus < 2 and status in ("Open", "Approved")
		and to_date >= %(from_date)s and from_date <= %(to_date)s
		and name != %(name)s""", {
			"employee": employee,
			"from_date": from_date,
			"to_date": to_date,
			"name": name
		}, as_dict = 1):

		if cint(half_day)==1 and getdate(half_day_date) == getdate(d.half_day_date) and (
			flt(total_leave_days)==0.5
			or getdate(from_date) == getdate(d.to_date)
			or getdate(to_date) == getdate(d.from_date)):

			total_leaves_on_half_day = _get_total_leaves_on_half_day(employee,half_day_date,name)
			if total_leaves_on_half_day >= 1:
				return _throw_overlap_error(employee, d)
		else:
			return _throw_overlap_error(employee, d)

	return ""

def validate_max_days(total_leave_days, leave_type):
	max_days = frappe.db.get_value("Leave Type", leave_type, "max_days_allowed")
	if max_days and total_leave_days > cint(max_days):
		return "Leave of type {0} cannot be longer than {1}".format(leave_type, max_days)
	return ""

def show_block_day_warning(employee,company,from_date, to_date):
	block_dates = get_applicable_block_dates(from_date, to_date, employee, company, all_lists=True)
	if block_dates:
		warning = "Warning: Leave application contains following block dates\n"
		for d in block_dates:
			warning += formatdate(d.block_date) + ": " + d.reason
		return warning
	return ""

def validate_block_days(employee,company,from_date, to_date,status):
	block_dates = get_applicable_block_dates(from_date, to_date,employee, company)

	if block_dates and status == "Approved":
		return "You are not authorized to approve leaves on Block Dates"
	return ""

def validate_salary_processed_days(employee,leave_type, from_date,to_date):
	if not frappe.db.get_value("Leave Type", leave_type, "is_lwp"):
		return ""

	last_processed_pay_slip = frappe.db.sql("""
		select start_date, end_date from `tabSalary Slip`
		where docstatus = 1 and employee = %s
		and ((%s between start_date and end_date) or (%s between start_date and end_date))
		order by modified desc limit 1
	""",(employee, to_date, from_date))

	if last_processed_pay_slip:
		return "Salary already processed for period between {0} and {1}, Leave application period cannot be between this date range.".format(formatdate(last_processed_pay_slip[0][0]),formatdate(last_processed_pay_slip[0][1]))
	return ""

def validate_leave_approver(employee,leave_approver,docstatus):
	e = frappe.get_doc("Employee", employee)
	leave_approvers = [l.leave_approver for l in e.get("leave_approvers")]

	if len(leave_approvers) and leave_approver not in leave_approvers:
		return "Leave approver must be one of {0}".format(comma_or(leave_approvers))

	elif leave_approver and not frappe.db.sql("""select name from `tabHas Role`
		where parent=%s and role='Leave Approver'""", leave_approver):
		return "{0} ({1}) must have role 'Leave Approver'".format(get_fullname(leave_approver), leave_approver)

	elif docstatus==1 and len(leave_approvers) and leave_approver != frappe.session.user:
		return "Only the selected Leave Approver can submit this Leave Application"
	return ""

def validate_attendance(employee, from_date, to_date):
	attendance = frappe.db.sql("""select name from `tabAttendance` where employee = %s and (attendance_date between %s and %s)
				and status = "Present" and docstatus = 1""",
		(employee, from_date, to_date))
	if attendance:
		return "Attendance for employee {0} is already marked for this day".format(employee)
	return ""

#VALIDATION SFA METHOD
def has_product_bundle(item_code):
	return frappe.db.sql("""select name from `tabProduct Bundle`
		where new_item_code=%s and docstatus != 2""", item_code)

def product_bundle_has_stock_item(product_bundle):
	"""Returns true if product bundle has stock item"""
	ret = len(frappe.db.sql("""select i.name from tabItem i, `tabProduct Bundle Item` pbi
		where pbi.parent = %s and pbi.item_code = i.name and i.is_stock_item = 1""", product_bundle))
	return ret

def validate_warehouse(items):
	for d in items:
		if (frappe.db.get_value("Item", d.item_code, "is_stock_item") == 1 or
			(has_product_bundle(d.item_code) and product_bundle_has_stock_item(d.item_code))) \
			and not d.warehouse and not cint(d.delivered_by_supplier):
			return "Delivery warehouse required for stock item {0}".format(d.item_code)
	return ""



@frappe.whitelist(allow_guest=True)
def me():
	me = frappe.session
	return me


@frappe.whitelist(allow_guest=True)
def ping():
	return "pong"

# USER PERMISSION
@frappe.whitelist(allow_guest=False)
def get_user_permission():
	user = frappe.session.user

	data = dict()
	data['has_roles'] = frappe.db.sql("SELECT * FROM `tabHas Role` WHERE parent='{}'".format(user),as_dict=1)
	dataUser = frappe.db.sql("SELECT * FROM `tabUser` WHERE name='{}'".format(user),as_dict=1)
	if len(dataUser) > 0:
		data['user'] = dataUser[0] 
	data['user_permissions'] = frappe.db.sql("SELECT * FROM `tabUser Permission` WHERE user='{}'".format(user),as_dict=1)

	return data

# METADATA
@frappe.whitelist(allow_guest=False)
def get_metadata(employee='%',company='',approver='%'):
	user = frappe.session.user
	data = dict()

	#daily net expense claim
	fetchNetSales = frappe.db.sql("SELECT SUM(total_claimed_amount) as net_expense_claim, posting_date FROM `tabExpense Claim` WHERE employee LIKE '{}' AND company = '{}' AND approval_status='Approved' GROUP BY posting_date DESC LIMIT 7".format(employee,company),as_dict=1)
	data['daily_net_expense_claim'] = fetchNetSales

	#leave application
	status = ["Open","Approved"]
	data['leave_application'] = dict()
	dataLA = data['leave_application']
	dataLA['count'] = dict()
	dataCount = dataLA['count']
	for stat in status:
		fetch = frappe.db.sql("SELECT COUNT(name) FROM `tabLeave Application` WHERE status='{}' AND employee LIKE '{}' AND company = '{}' ORDER BY modified".format(stat,employee,company),as_list=1)
		if (len(fetch) > 0):
			firstFetch = fetch[0]
			dataCount[stat] = firstFetch[0]

	#employee advance
	status = ['Draft','Unpaid','Claimed','Paid']
	data['employee_advance'] = dict()
	dataEA = data['employee_advance']
	dataEA['count'] = dict()
	dataCount = dataEA['count']
	for stat in status:
		fetch = frappe.db.sql("SELECT COUNT(name) FROM `tabEmployee Advance` WHERE status='{}' AND employee LIKE '{}' AND company = '{}' ORDER BY modified".format(stat,employee,company),as_list=1)
		if (len(fetch) > 0):
			firstFetch = fetch[0]
			dataCount[stat] = firstFetch[0]

	#expense claim
	status = ['Draft','Unpaid','Paid']
	data['expense_claim'] = dict()
	dataEC = data['expense_claim']
	dataEC['count'] = dict()
	dataCount = dataEC['count']
	for stat in status:
		fetch = frappe.db.sql("SELECT COUNT(name) FROM `tabExpense Claim` WHERE status='{}' AND employee LIKE '{}' AND company = '{}' ORDER BY modified".format(stat,employee,company),as_list=1)
		if (len(fetch) > 0):
			firstFetch = fetch[0]
			dataCount[stat] = firstFetch[0]

	#sales order
	status = ['Draft', 'To Deliver and Bill','To Bill','To Deliver','Completed','Cancelled','Closed']
	data['sales_order'] = dict()
	dataSO = data['sales_order']
	dataSO['count'] = dict()
	dataCount = dataSO['count']
	for stat in status:
		fetch = frappe.db.sql("SELECT COUNT(name) FROM `tabSales Order` WHERE owner = '{}' AND status='{}' ORDER BY modified".format(user, stat),as_list=1)
		if (len(fetch) > 0):
			firstFetch = fetch[0]
			dataCount[stat] = firstFetch[0]

	#invoice
	status = ['Overdue','Unpaid','Paid']
	data['invoice'] = dict()
	dataINV = data['invoice']
	dataINV['count'] = dict()
	dataCount = dataINV['count']
	for stat in status:
		fetch = frappe.db.sql("SELECT COUNT(name) FROM `tabSales Invoice` WHERE owner = '{}' AND status='{}' ORDER BY modified".format(user, stat),as_list=1)
		if (len(fetch) > 0):
			firstFetch = fetch[0]
			dataCount[stat] = firstFetch[0]

	#lead
	status = ['Lead','Open','Replied','Opportunity','Interested','Quotation','Lost Quotation','Converted','Do Not Contact']
	data['lead'] = dict()
	dataLead = data['lead']
	dataLead['count'] = dict()
	dataCount = dataLead['count']
	for stat in status:
		fetch = frappe.db.sql("SELECT COUNT(name) FROM `tabLead` WHERE owner = '{}' AND status='{}' ORDER BY modified".format(user, stat),as_list=1)
		if (len(fetch) > 0):
			firstFetch = fetch[0]
			dataCount[stat] = firstFetch[0]

	dataCount['Quotation'] += len(frappe.db.sql("SELECT name FROM `tabQuotation` WHERE owner = '{}' AND (status = 'Submitted' OR status='Open') AND quotation_to = 'Customer'".format(user),as_dict=1))
	dataCount['Converted'] += len(frappe.db.sql("SELECT * FROM `tabQuotation` WHERE owner = '{}' AND status = 'Ordered' AND quotation_to = 'Customer'".format(user),as_dict=1))
	dataCount['Opportunity'] += len(frappe.db.sql("SELECT * FROM `tabOpportunity` WHERE owner = '{}' AND (status != 'Converted' AND status != 'Lost' AND status != 'Quotation') AND enquiry_from = 'Customer'".format(user),as_dict=1))

	#net sales
	fetchNetSales = frappe.db.sql("SELECT SUM(grand_total) as net_sales,DATE_FORMAT(creation, '%e %M %Y') as posting_date FROM `tabSales Order` WHERE owner='{}' AND docstatus != 2 GROUP BY DATE_FORMAT(creation, '%e %M %Y') ORDER BY DATE_FORMAT(creation, '%e %M %Y') DESC LIMIT 7".format(user),as_dict=1)
	data['daily_net_sales'] = fetchNetSales

	return data

# LEAVE APPLICATION
@frappe.whitelist(allow_guest=False)
def get_leave_allocation(status='',query='',sort='',page=0):
	filters = ['name','leave_type','employee_name']
	n_filters = len(filters)
	generate_filters = ""
	for i in range(0,n_filters-1):
		generate_filters += "{} LIKE '%{}%' OR ".format(filters[i],query)
	generate_filters += "{} LIKE '%{}%' ".format(filters[n_filters-1],query)

	statuses = status.split(',')
	generate_status = "'" + "','".join(statuses) + "'"

	sortedby = 'modified'
	if (sort != ''):
		sortedby = sort

	data = frappe.db.sql("SELECT * FROM `tabLeave Allocation` WHERE docstatus = 1 AND status IN ({}) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(generate_status,generate_filters,sortedby,page),as_dict=1)

	return data

@frappe.whitelist(allow_guest=False)
def request_leave_application(employee='',company='',leave_type='', from_date='', to_date='', status='Open', half_day=0, half_day_date='',docstatus=0,leave_approver=None):
	error_message = []
	warning_message = []
	total_leave_days = 0

	#VALIDATION
	if not is_lwp(leave_type):
		result = validate_dates_acorss_allocation(employee, leave_type, from_date, to_date)
		if result != "":
			error_message.append(result)
		result = validate_back_dated_application(employee, leave_type, to_date)
		if result != "":
			error_message.append(result)
	result = validate_balance_leaves(employee, leave_type, from_date, to_date, half_day, half_day_date, status)
	if str(type(result)) == "<type 'int'>":
		total_leave_days = result
	else:
		error_message.append(result)

	result = validate_leave_overlap(total_leave_days,employee,from_date,to_date,half_day,half_day_date)
	if result != "":
		error_message.append(result)
	result = validate_max_days(total_leave_days, leave_type)
	if result != "":
		error_message.append(result)
	result = show_block_day_warning(employee,company,from_date,to_date)
	if result != "":
		warning_message.append(result)
	result = validate_block_days(employee,company,from_date, to_date,status)
	if result != "":
		error_message.append(result)
	result = validate_salary_processed_days(employee,leave_type, from_date,to_date)
	if result != "":
		error_message.append(result)
	result = validate_leave_approver(employee,leave_approver,docstatus)
	if result != "":
		error_message.append(result)
	result = validate_attendance(employee, from_date, to_date)
	if result != "":
		error_message.append(result)
		
	data = dict()
	data['warning_message'] = []
	data['error_message'] = []
	if (len(warning_message) > 0):
		data['result'] = 'success with some warning'
		data['warning_message'] = warning_message
	if (len(error_message) > 0):
		data['result'] = "not success"
		data['error_message'] = error_message
	else:
		data['result'] = "success"
	return data

@frappe.whitelist(allow_guest=False)
def get_leave_application(leave_approver='%',filter_requested='all',employee='%',company='',status='',query='',sort='',page=0):
	filters = ['name','leave_type','employee_name']
	n_filters = len(filters)
	generate_filters = ""
	for i in range(0,n_filters-1):
		generate_filters += "{} LIKE '%{}%' OR ".format(filters[i],query)
	generate_filters += "{} LIKE '%{}%' ".format(filters[n_filters-1],query)

	statuses = status.split(',')
	generate_status = "'" + "','".join(statuses) + "'"

	sortedby = 'modified'
	if (sort != ''):
		sortedby = sort

	if filter_requested == 'me':
		query = "SELECT * FROM `tabLeave Application` WHERE employee LIKE '{}' AND company = '{}' AND status IN ({}) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(employee, company,generate_status,generate_filters,sortedby,page)
	elif filter_requested == 'other':
		query = "SELECT * FROM `tabLeave Application` WHERE leave_approver LIKE '{}' AND company = '{}' AND status IN ({}) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(leave_approver, company,generate_status,generate_filters,sortedby,page)
	elif filter_requested == 'all':
		query = "SELECT * FROM `tabLeave Application` WHERE (employee LIKE '{}' OR leave_approver LIKE '{}') AND company = '{}' AND status IN ({}) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(employee, leave_approver, company,generate_status,generate_filters,sortedby,page)

	data = frappe.db.sql(query,as_dict=1)

	return data

@frappe.whitelist(allow_guest=False)
def get_leave_approver(employee=''):
	data = frappe.db.sql("SELECT * FROM `tabEmployee Leave Approver` WHERE parent = '{}' AND parentfield = 'leave_approvers'".format(employee),as_dict=1)

	leave_approvers = "'"
	for d in data:
		leave_approvers += d["leave_approver"] + "','"

	data = frappe.db.sql("SELECT full_name, name FROM `tabUser` WHERE name IN ({}')".format(leave_approvers),as_dict=1)

	return data

# EXPENSE

def validate_expense_approver(exp_approver):
	if exp_approver and "Expense Approver" not in frappe.get_roles(exp_approver):
		return "{0} ({1}) must have role 'Expense Approver'".format(get_fullname(exp_approver), exp_approver)
	return ""

def validate_expense_account(expense_claim_type, company):
	account = frappe.db.get_value("Expense Claim Account",
		{"parent": expense_claim_type, "company": company}, "default_account")
	if not account:
		return expense_claim_type
	return ""



@frappe.whitelist(allow_guest=False)
def request_expense_claim(exp_approver='', company='',expense_claim_type=''):
	error_message = []
	warning_message = []
	total_leave_days = 0

	result = validate_expense_approver(exp_approver)
	if result != "":
		error_message.append(result)
	ects = expense_claim_type.split(',')
	error_ects = []
	for ect in ects:
		result = validate_expense_account(ect,company)
		if result != "":
			error_ects.append(result)

	if len(error_ects) > 0:
		generated_error_ects = ",".join(error_ects)
		error_message.append("Please set default account in Expense Claim Type {0}".format(generated_error_ects))
		
	data = dict()
	data['warning_message'] = []
	data['error_message'] = []
	if (len(warning_message) > 0):
		data['result'] = 'success with some warning'
		data['warning_message'] = warning_message
	if (len(error_message) > 0):
		data['result'] = "not success"
		data['error_message'] = error_message
	else:
		data['result'] = "success"
	return data


@frappe.whitelist(allow_guest=False)
def get_expense_claim(exp_approver='%',filter_requested='all',employee='%',company='',status='',approval_status='',query='',sort='',page=0):
	filters = ['name','employee_name']
	n_filters = len(filters)
	generate_filters = ""
	for i in range(0,n_filters-1):
		generate_filters += "{} LIKE '%{}%' OR ".format(filters[i],query)
	generate_filters += "{} LIKE '%{}%' ".format(filters[n_filters-1],query)

	approval_statuses = approval_status.split(',')
	generate_approval_status = "'" + "','".join(approval_statuses) + "'"
	statuses = status.split(',')
	generate_status = "'" + "','".join(statuses) + "'"

	sortedby = 'modified'
	if (sort != ''):
		sortedby = sort

	if filter_requested == 'me':
		query = "SELECT * FROM `tabExpense Claim` WHERE employee LIKE '{}' AND company = '{}' AND (status IN ({}) AND approval_status IN ({})) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(employee,company,generate_status,generate_approval_status,generate_filters,sortedby,page)
	elif filter_requested == 'other':
		query = "SELECT * FROM `tabExpense Claim` WHERE exp_approver LIKE '{}' AND company = '{}' AND (status IN ({}) AND approval_status IN ({})) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(exp_approver,company,generate_status,generate_approval_status,generate_filters,sortedby,page)
	elif filter_requested == 'all':
		query = "SELECT * FROM `tabExpense Claim` WHERE (exp_approver LIKE '{}' OR employee LIKE '{}') AND company = '{}' AND (status IN ({}) AND approval_status IN ({})) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(exp_approver, employee,company,generate_status,generate_approval_status,generate_filters,sortedby,page)
	data = frappe.db.sql(query,as_dict=1)

	return data


@frappe.whitelist(allow_guest=True)
def attach_image_to_expense_claim():
	response = {}

	validate = validate_method(frappe.local.request.method,["POST"])
	if validate != True:
		return validate

	req = frappe.local.form_dict

	hash = hashlib.sha1()
	hash.update(str(time.time()))
	hash_now = hash.hexdigest()[:10]
	req.filename = "attachment_{}.jpg".format(hash_now)




	data = json.loads(req.data)
	req.filedata = data['filedata']
	req.expense_claim = data['expense_claim']

	try:

		uploaded = upload("Expense Claim",req.expense_claim,1)

		response["code"] = 200
		response["message"] = "Success"
		response["data"] = uploaded

	except Exception as e:
		response["code"] = 400
		response["message"] = e.message
		response["data"] = ""
	except UnboundLocalError as e:
		response["code"] = 400
		response["message"] = e.message
		response["data"] = ""

	return response


@frappe.whitelist(allow_guest=False)
def get_expense_approver():
	user = frappe.session.user
	data = frappe.db.sql("SELECT parent FROM `tabHas Role` WHERE role ='Expense Approver'".format(user),as_dict=1)

	exp_approvers = "'"
	for d in data:
		exp_approvers += d["parent"] + "','"

	data = frappe.db.sql("SELECT full_name, name FROM `tabUser` WHERE name IN ({}')".format(exp_approvers),as_dict=1)

	return data

@frappe.whitelist(allow_guest=False)
def approve_expense_claim(approve='',is_paid='',name=''):
	status = 'Draft'
	approval_status = 'Draft'
	if approve == '1':
		approval_status = 'Approved'
		if is_paid == '1':
			status = 'Paid'
		else:
			status = 'Unpaid'
	else:
		approval_status = 'Rejected'
		status = 'Rejected'

	result = frappe.db.sql("UPDATE `tabExpense Claim` SET status = '{}', docstatus=1, approval_status = '{}' WHERE name = '{}'".format(status, approval_status, name))
	frappe.db.commit()
	return result

# EMPLOYEE ADVANCE

@frappe.whitelist(allow_guest=False)
def get_employee_advance(owner='%',employee='%', company='', status='',query='',sort='',page=0):
	filters = ['name','employee_name','purpose']
	n_filters = len(filters)
	generate_filters = ""
	for i in range(0,n_filters-1):
		generate_filters += "{} LIKE '%{}%' OR ".format(filters[i],query)
	generate_filters += "{} LIKE '%{}%' ".format(filters[n_filters-1],query)

	statuses = status.split(',')
	generate_status = "'" + "','".join(statuses) + "'"

	sortedby = 'modified'
	if (sort != ''):
		sortedby = sort



	data = frappe.db.sql("SELECT * FROM `tabEmployee Advance` WHERE (owner LIKE '{}' OR employee LIKE '{}') AND company='{}' AND status IN ({}) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(owner,employee,company,generate_status,generate_filters,sortedby,page),as_dict=1)
		 
	return data

@frappe.whitelist(allow_guest=False)
def get_sales_order(status='',query='',sort='',page=0):
	user = frappe.session.user
	filters = ['name','customer_name']
	n_filters = len(filters)
	generate_filters = ""
	for i in range(0,n_filters-1):
		generate_filters += "{} LIKE '%{}%' OR ".format(filters[i],query)
	generate_filters += "{} LIKE '%{}%' ".format(filters[n_filters-1],query)

	statuses = status.split(',')
	generate_status = "'" + "','".join(statuses) + "'"

	sortedby = 'modified'
	if (sort != ''):
		sortedby = sort

	# data = frappe.get_list("Sales Order", 
	# 						fields="*", 
	# 						filters = 
	# 						{
	# 							"docstatus": 1,
	# 							"status": ("IN", statuses),
	# 							"name": ("LIKE", "%" + query + "%")
	# 							# "customer_name": ("LIKE","%" +query + "%")
	# 						})
	data = frappe.db.sql("SELECT * FROM `tabSales Order` WHERE docstatus = 1 AND status IN ({}) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(generate_status,generate_filters,sortedby,page),as_dict=1)
	return data

@frappe.whitelist(allow_guest=False)
def validate_sales_order(items):
	return validate_warehouse(items)

@frappe.whitelist(allow_guest=False)
def get_sales_invoice(status='',query='',sort='',page=0):
	user = frappe.session.user
	filters = ['name','customer_name']
	n_filters = len(filters)
	generate_filters = ""
	for i in range(0,n_filters-1):
		generate_filters += "{} LIKE '%{}%' OR ".format(filters[i],query)
	generate_filters += "{} LIKE '%{}%' ".format(filters[n_filters-1],query)

	statuses = status.split(',')
	generate_status = "'" + "','".join(statuses) + "'"

	sortedby = 'modified'
	if (sort != ''):
		sortedby = sort

	data = frappe.db.sql("SELECT * FROM `tabSales Invoice` WHERE docstatus = 1 AND owner = '{}' AND status IN ({}) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(user,generate_status,generate_filters,sortedby,page),as_dict=1)

	return data

@frappe.whitelist(allow_guest=False)
def get_item(is_sales_item='1',is_stock_item='1',ref='',page='0'):
	data = frappe.db.sql("SELECT * FROM `tabItem` WHERE is_sales_item = {} AND is_stock_item = {} AND (item_name LIKE '{}%' OR item_code LIKE '{}%') LIMIT 20 OFFSET {}".format(is_sales_item, is_stock_item,ref,ref,page),as_dict=1)

	for row in data:
		row['product_bundle_item'] = list("")
		if (row['is_stock_item'] == 0):
			fetchBundleItem = frappe.db.sql("SELECT * FROM `tabProduct Bundle Item` WHERE parent = '{}'".format(row['item_code']),as_dict=True)
			row['product_bundle_item'] = fetchBundleItem
	return data

@frappe.whitelist(allow_guest=False)
def get_lead(status='',query='',sort='',page=0):
	user = frappe.session.user
	statuses = status.split(',')
	generate_status = "'" + "','".join(statuses) + "'"

	sortedby = 'modified'
	if (sort != ''):
		sortedby = sort

	#lead
	filters = ['name','company_name','lead_name','email_id']
	n_filters = len(filters)
	generate_filters = ""
	for i in range(0,n_filters-1):
		generate_filters += "{} LIKE '%{}%' OR ".format(filters[i],query)
	generate_filters += "{} LIKE '%{}%' ".format(filters[n_filters-1],query)
	data = dict()
	data['leads'] = frappe.db.sql("SELECT * FROM `tabLead` WHERE owner = '{}' AND status IN ({}) AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(user, generate_status,generate_filters,sortedby,page),as_dict=1)

	#quotation
	filters = ['name','customer_name','contact_email']
	n_filters = len(filters)
	generate_filters = ""
	for i in range(0,n_filters-1):
		generate_filters += "{} LIKE '%{}%' OR ".format(filters[i],query)
	generate_filters += "{} LIKE '%{}%' ".format(filters[n_filters-1],query)

	if 'Quotation' in statuses:
		data['quotations'] = frappe.db.sql("SELECT * FROM `tabQuotation` WHERE owner = '{}' AND (status = 'Submitted' OR status='Open') AND quotation_to = 'Customer' AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(user, generate_filters,sortedby,page),as_dict=1)
	elif 'Converted' in statuses:
		data['quotations'] = frappe.db.sql("SELECT * FROM `tabQuotation` WHERE owner = '{}' AND status = 'Ordered' AND quotation_to = 'Customer' AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(user, generate_filters,sortedby,page),as_dict=1)
	else:
		data['quotations'] = list("")

	#opportunity
	if 'Opportunity' in statuses:
		data['opportunities'] = frappe.db.sql("SELECT * FROM `tabOpportunity` WHERE owner = '{}' AND status != 'Converted' AND status != 'Lost' AND status != 'Quotation' AND enquiry_from = 'Customer' AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(user, generate_filters,sortedby,page),as_dict=1)
	# data['query'] = "SELECT * FROM `tabOpportunity` WHERE owner = '{}' AND status != 'Converted' AND status != 'Lost' AND status != 'Quotation' AND enquiry_from = 'Customer' AND ({}) ORDER BY {} DESC, status ASC LIMIT 20 OFFSET {}".format(user, generate_filters,sortedby,page)
	return data

@frappe.whitelist(allow_guest=False)
def get_lead_item(lead_no=''):
	fetch_opportunity = frappe.db.sql("SELECT * FROM `tabOpportunity` WHERE lead = '{}'".format(lead_no),as_dict=1)
	fetch_quotation = frappe.db.sql("SELECT * FROM `tabQuotation` WHERE lead = '{}'".format(lead_no),as_dict=1)
	data = dict()
	data['opportunity'] = fetch_opportunity
	data['quotation'] = fetch_quotation
	return data


@frappe.whitelist(allow_guest=False)
def get_user():
	data = frappe.db.sql("SELECT * FROM `tabUser` WHERE name != 'Administrator'",as_dict=1)
	return data

