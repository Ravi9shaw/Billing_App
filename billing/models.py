"""Validate request shapes before entering a database transaction."""

from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, Field, StrictInt

Amount = Annotated[Decimal, Field(ge=0, le=999999999, allow_inf_nan=False)]


class Line(BaseModel):
    item_id: StrictInt
    version: StrictInt
    quantity: Annotated[StrictInt, Field(ge=1, le=100000)]
    rate: Amount | None = None
    discount: Amount = Decimal(0)
    gst_rate: Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)] | None = None


class NewCustomer(BaseModel):
    name: str = Field(default="", max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)


class BillBody(BaseModel):
    items: list[Line] = Field(min_length=1, max_length=500)
    customer_id: StrictInt | None = None
    customer: NewCustomer = Field(default_factory=NewCustomer)
    bill_date: str | None = None
    tax_mode: Literal["inclusive", "exclusive", "none"] | None = None
    interstate: bool = False
    payment_mode: Literal["Cash", "Card", "UPI", "Other"] = "Cash"
    paid_now: Amount | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    version: StrictInt | None = None


class ItemBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category_id: StrictInt
    subtype_id: StrictInt | None = None
    barcode: str | None = Field(default=None, max_length=200)
    size: str | None = Field(default=None, max_length=100)
    color: str | None = Field(default=None, max_length=100)
    hsn: str | None = Field(default=None, max_length=100)
    rate: Amount
    gst_rate: Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)] = Decimal(5)
    stock_qty: Annotated[StrictInt, Field(ge=0, le=999999999)] = 0
    active: Literal[0, 1] = 1
    version: StrictInt | None = None
    expected_stock: StrictInt | None = None


class CustomerBody(NewCustomer):
    name: str = Field(min_length=1, max_length=200)
    gstin: str | None = Field(default=None, max_length=100)
    pin_code: str | None = Field(default=None, max_length=40)
    whatsapp_opt_in: Literal[0, 1] = 0
    version: StrictInt | None = None


class PaymentBody(BaseModel):
    amount: Amount
    payment_date: str | None = None
    payment_mode: Literal["Cash", "Card", "UPI", "Other"] = "Cash"
    notes: str = Field(default="", max_length=2000)


class ConfigurationBody(BaseModel):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: dict[str, str]
    whatsapp_token: str = Field(default="", max_length=4000)
    clear_whatsapp_token: bool = False
    new_password: str = Field(default="", max_length=1000)
    current_password: str = Field(default="", max_length=1000)
    confirm_password: str = Field(default="", max_length=1000)
