import math


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def add_sample(samples, now, value, window):
    value = number(value)
    if value is not None and (not samples or now - samples[-1][0] >= 10):
        samples.append([now, value])
    samples[:] = [sample for sample in samples if now - window <= sample[0] <= now]


def slope(samples, now, window, count=2, span=0):
    kept = [(at, value) for at, value in samples if now - window <= at <= now]
    if len(kept) < count or kept[-1][0] - kept[0][0] < span:
        return None
    x = [(at - kept[0][0]) / 60 for at, _ in kept]
    y = [value for _, value in kept]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    denominator = sum((at - mx) ** 2 for at in x)
    if denominator == 0:
        return None
    return sum((at - mx) * (value - my) for at, value in zip(x, y)) / denominator


def bath_eta(samples, now, current, target, previous_rate=None):
    rate = slope(samples, now, 300, count=4)
    if rate is None or rate <= 0.1:
        rate = number(previous_rate)
    if rate is None or rate <= 0.1 or current is None or target is None:
        return None, None
    return now + max(0, target - current) / rate * 60, rate


def probe_eta(samples, now, current, pull):
    rate = slope(samples, now, 600, count=2, span=300)
    if rate is None or rate <= 0.2 or current is None:
        return None, rate
    return now + max(0, pull - current) / rate * 60, rate
