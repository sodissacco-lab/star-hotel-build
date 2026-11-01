"""Amounts in words for receipts (Uganda shillings)."""
from decimal import Decimal

ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
        "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = [(10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (10 ** 3, "thousand")]


def _below_thousand(n):
    parts = []
    if n >= 100:
        parts.append(ONES[n // 100] + " hundred")
        n %= 100
        if n:
            parts.append("and")
    if n >= 20:
        parts.append(TENS[n // 10] + ("-" + ONES[n % 10] if n % 10 else ""))
    elif n:
        parts.append(ONES[n])
    return " ".join(parts)


def number_to_words(n):
    if n == 0:
        return "zero"
    parts = []
    for size, name in SCALES:
        if n >= size:
            parts.append(_below_thousand(n // size) + " " + name)
            n %= size
    if n:
        # "and" before a final hundreds-less group, e.g. "one million and fifty"
        tail = _below_thousand(n)
        parts.append(("and " if parts and n < 100 else "") + tail)
    return " ".join(parts)


def amount_in_words(amount):
    amount = Decimal(amount).quantize(Decimal("0.01"))
    shillings, cents = int(amount), int((amount - int(amount)) * 100)
    text = number_to_words(shillings)
    text = text[0].upper() + text[1:] + " Uganda shillings"
    if cents:
        text += f" and {number_to_words(cents)} cents"
    return text + " only"
