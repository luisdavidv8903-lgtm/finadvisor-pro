from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, condecimal


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class OrderCreate(BaseModel):
    crypto_amount: condecimal(gt=Decimal("0"), decimal_places=8)
    fiat_amount: condecimal(gt=Decimal("0"), decimal_places=2)
    fiat_currency: str = Field(min_length=3, max_length=8)
    payment_method: str = Field(min_length=2, max_length=64)
    # seller_id NUNCA se acepta del cliente: se toma de current_user en el endpoint.
