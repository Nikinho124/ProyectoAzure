from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def moneda_chilena(valor):
    try:
        monto = Decimal(valor)
    except (InvalidOperation, TypeError, ValueError):
        return "Precio por configurar"
    if not monto.is_finite():
        return "Precio por configurar"
    return f"${monto:,.0f}".replace(",", ".")
