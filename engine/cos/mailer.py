"""Outlook COM send."""
import pythoncom
import win32com.client

from . import config


def send(subject: str, html_body: str, to: str | None = None) -> None:
    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.To = to or config.USER_EMAIL
        mail.Subject = subject
        mail.HTMLBody = html_body
        mail.Send()
    finally:
        pythoncom.CoUninitialize()
