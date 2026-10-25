"""One billing policy: exact time arithmetic, then half-up rounding to cents per day."""
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP


def decimal(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("billing values must be finite numbers")
    return result


def cents(value) -> float:
    return float(decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def bill_day(seconds: int, rounding_minutes: int, rate: float) -> tuple[float, float]:
    billed = decimal(seconds)
    if rounding_minutes > 0:
        step = decimal(rounding_minutes) * 60
        billed = (billed / step).to_integral_value(rounding=ROUND_CEILING) * step
    hours = billed / 3600
    return float(hours), cents(hours * decimal(rate))
