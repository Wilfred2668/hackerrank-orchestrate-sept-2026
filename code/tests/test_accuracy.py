"""Independent financial-rule regressions; no sample labels or IDs."""
from datetime import date
from decimal import Decimal
from lib.models import FinancialEvent
from lib.reconciliation import infer_salary_payday


def payroll(month, day, status="settled", settlement=None, description="Payroll"):
    return FinancialEvent(event_id=f"pay_{month}_{day}", user_id="synthetic",
        event_type="income", description=description, category="salary", direction="credit",
        amount=Decimal("3000"), currency="USD", event_date=date(2025, month, day),
        settlement_date=settlement or date(2025, month, day), status=status,
        linked_event_id=None, flexibility="fixed", minimum_allowed_amount=None)


def test_isolated_salary_receipt_does_not_replace_established_payday():
    events = [payroll(m, 12) for m in range(1, 7)] + [payroll(6, 28)]
    assert infer_salary_payday(events) == 12


def test_confirmed_schedule_overrides_historical_payday_using_settlement():
    events = [payroll(m, 12) for m in range(1, 7)]
    events.append(payroll(7, 12, "scheduled", date(2025, 7, 19)))
    assert infer_salary_payday(events) == 19


def test_commission_does_not_replace_regular_payday():
    events = [payroll(m, 12) for m in range(1, 7)]
    events.append(payroll(6, 28, description="Sales commission"))
    assert infer_salary_payday(events) == 12
