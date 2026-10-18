import sys
import traceback
import functools
from src.utils.alert_publisher import push_alert
from src.utils.tg_notifier import send_tg_event

def report_fault(source: str, exc: BaseException):
    tb_short = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    msg = f"⚠️ <b>FAULT [{source}]</b>: <code>{tb_short[:220]}</code>"
    try:
        push_alert(1, source, msg)
    except Exception:
        pass
    try:
        send_tg_event(source, "ERROR", tb_short[:220])
    except Exception:
        pass

def install_global_exception_hook(source_name: str):
    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        report_fault(source_name, exc_value)
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _hook

def fault_tolerant(source_name: str, default_return=None):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                report_fault(source_name, e)
                return default_return
        return wrapper
    return decorator
