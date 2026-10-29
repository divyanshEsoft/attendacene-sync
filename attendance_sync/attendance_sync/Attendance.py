import json
from datetime import datetime

import frappe
import requests
from frappe import _
from frappe.utils.password import get_decrypted_password
from hrms.hr.doctype.employee_checkin.employee_checkin import add_log_based_on_employee_field
from requests.auth import HTTPDigestAuth


class Attendance:
	def __init__(self, device_ip, major, minor, device_user, device_password):
		self.device_ip = device_ip
		self.attendance_depth = 30
		self.major = int(major)
		self.minor = int(minor)
		self.device_user = device_user
		self.docname = frappe.db.get_value("Device Configuration", {"device_ip": device_ip}, "name")
		if not self.docname:
			clean_ip = device_ip.replace("https://", "").replace("http://", "").strip("/")
			self.docname = frappe.db.get_value(
				"Device Configuration",
				{"device_ip": ["like", f"%{clean_ip}%"]},
				"name",
			)
		if not self.docname:
			self.docname = frappe.db.get_value("Device Configuration", {}, "name")

		self.device_user_password = get_decrypted_password(
			"Device Configuration", self.docname, "device_user_password"
		)

	def _format_time(self, time_obj):
		"""Helper function to format datetime to Device required format with timezone offset."""
		try:
			from zoneinfo import ZoneInfo

			if isinstance(time_obj, str):
				time_obj = datetime.fromisoformat(time_obj.replace("Z", "+00:00"))

			if time_obj.tzinfo is None:
				system_tz = frappe.utils.get_system_timezone() or "Asia/Kolkata"
				tz = ZoneInfo(system_tz)
				time_obj = time_obj.replace(tzinfo=tz)

			return time_obj.isoformat()
		except Exception:
			# frappe.log_error(frappe.get_traceback(), "Attendance: Time Formatting Failed")
			return None

	def fetch_all_attendance_logs(self, start_time, end_time):
		"""Fetch all logs from the device, handling pagination and HTTPS/Cloudflare tunnels."""
		device_host = str(self.device_ip).strip()
		if not device_host.startswith("http://") and not device_host.startswith("https://"):
			if "trycloudflare.com" in device_host:
				base_url = f"https://{device_host}"
			else:
				if ":" not in device_host:
					device_host = f"{device_host}:8000"
				base_url = f"http://{device_host}"
		else:
			base_url = device_host.rstrip("/")

		url = f"{base_url}/ISAPI/AccessControl/AcsEvent?format=json"

		all_records = []
		search_result_position = 0
		max_results = self.attendance_depth

		payload_template = {
			"AcsEventCond": {
				"searchID": "1",
				"maxResults": max_results,
				"major": self.major,
				"minor": self.minor,
				"startTime": start_time,
				"endTime": end_time,
			}
		}

		try:
			with requests.Session() as session:
				session.auth = HTTPDigestAuth(self.device_user, self.device_user_password)

				while True:
					payload = json.loads(json.dumps(payload_template))
					payload["AcsEventCond"]["searchResultPosition"] = search_result_position

					response = session.post(url, json=payload, timeout=30)
					response.raise_for_status()
					data = response.json()

					acs_event = data.get("AcsEvent", {})
					events = acs_event.get("InfoList", [])
					total_matches = int(acs_event.get("totalMatches", 0))
					returned = len(events)

					# print(f"\n===== BIOMETRIC DEVICE RESPONSE (matches: {returned}, total: {total_matches}) =====")
					# print(json.dumps(data, indent=2))
					# print("=========================================================================\n")

					if not events:
						break

					all_records.extend(events)

					if len(all_records) >= total_matches:
						break

					search_result_position += returned

					if acs_event.get("responseStatusStrg") != "MORE":
						break

		except requests.exceptions.RequestException as e:
			# print(f"[Attendance Sync] RequestException: {e}")
			# frappe.log_error(
			# 	message=frappe.get_traceback(),
			# 	title=f"Attendance Fetch Failed (Device: {self.device_ip})",
			# )
			pass
		except Exception as e:
			# print(f"[Attendance Sync] Unexpected Exception: {e}")
			# frappe.log_error(
			# 	message=frappe.get_traceback(),
			# 	title=f"Unexpected Error in Attendance Fetch (Device: {self.device_ip})",
			# )
			pass

		return all_records

	def get_employee_by_device_id(self, employee_no_string):
		"""Fetch employee details based on the attendance_device_id."""
		try:
			employee = frappe.db.get_value(
				"Employee",
				{"attendance_device_id": employee_no_string},
				["name", "employee_name"],
				as_dict=True,
			)
			return employee
		except Exception:
			# frappe.log_error(
			# 	message=frappe.get_traceback(),
			# 	title=f"Error fetching employee by device ID ({employee_no_string})",
			# )
			return None

	def process_logs(self, data):
		"""Process the logs and push every record to Employee Checkin."""
		if not data:
			return

		filtered_info_list = [item for item in data if "employeeNoString" in item]

		for record in filtered_info_list:
			try:
				employee_no_string = record["employeeNoString"]
				employee = self.get_employee_by_device_id(employee_no_string)
				if not employee:
					continue

				emp_no = employee["name"]
				timestamp = record.get("time", "")
				if not timestamp:
					continue

				timestamp_obj = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
				formatted_timestamp = timestamp_obj.strftime("%Y-%m-%d %H:%M:%S")

				self.log_employee_attendance(emp_no, record, formatted_timestamp)

			except Exception:
				# frappe.log_error(
				# 	message=frappe.get_traceback(),
				# 	title=f"Error processing attendance record ({record.get('employeeNoString', 'Unknown')})",
				# )
				continue

		try:
			now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
			shift_types = frappe.get_all("Shift Type", fields=["name"])
			for shift_type in shift_types:
				frappe.db.set_value("Shift Type", shift_type["name"], "last_sync_of_checkin", now)
			frappe.db.commit()
		except Exception:
			# frappe.log_error(
			# 	message=frappe.get_traceback(),
			# 	title="Error updating last_sync_of_checkin in Shift Type",
			# )
			pass

	def log_employee_attendance(self, emp_no, record, formatted_timestamp):
		"""Log employee IN and OUT times based on individual record."""
		try:
			if not self.check_duplicate_checkin(emp_no, formatted_timestamp):
				add_log_based_on_employee_field(
					employee_field_value=emp_no,
					timestamp=formatted_timestamp,
					employee_fieldname="name",
					device_id=record["employeeNoString"],
				)
		except Exception:
			# frappe.log_error(
			# 	message=frappe.get_traceback(),
			# 	title=f"Error logging employee checkin (Employee: {emp_no})",
			# )
			pass

	def check_duplicate_checkin(self, emp_no, timestamp):
		"""Check if an Employee Checkin exists with the same employee and timestamp."""
		try:
			return frappe.db.exists("Employee Checkin", {"employee": emp_no, "time": timestamp})
		except Exception:
			# frappe.log_error(
			# 	message=frappe.get_traceback(),
			# 	title=f"Error checking duplicate checkin (Employee: {emp_no})",
			# )
			return False

	def get_and_process_attendance(self, start_time, end_time):
		"""Main method to fetch and process attendance logs."""
		try:
			data = self.fetch_all_attendance_logs(self._format_time(start_time), self._format_time(end_time))
			if data:
				self.process_logs(data)
		except Exception:
			# frappe.log_error(
			# 	message=frappe.get_traceback(),
			# 	title=f"Attendance Sync Failed (Device: {self.device_ip})",
			# )
			pass


@frappe.whitelist()
def process_attendance_in_background(
	start_date, end_date, device_ip, major, minor, device_user, device_user_password
):
	# print(f"\n[Attendance Sync] Background job started for device: {device_ip} ({start_date} to {end_date})")
	try:
		attendance = Attendance(
			device_ip=device_ip,
			major=major,
			minor=minor,
			device_user=device_user,
			device_password=device_user_password,
		)
		attendance.get_and_process_attendance(start_date, end_date)
		# print(f"[Attendance Sync] Background job completed successfully for device: {device_ip}\n")
	except Exception as e:
		# print(f"[Attendance Sync] Background job failed: {e}\n")
		# frappe.log_error(
		# 	message=frappe.get_traceback(),
		# 	title="Attendance Background Job Failed",
		# )
		pass
