from decimal import Decimal

from pydantic import BaseModel, Field, condecimal


class NonceRequest(BaseModel):
    wallet_address: str = Field(min_length=25, max_length=64)


class NonceResponse(BaseModel):
    nonce: str
    message: str  # el mensaje exacto que el cliente debe firmar


class VerifyRequest(BaseModel):
    wallet_address: str = Field(min_length=25, max_length=64)
    signature: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class OrderMetadataCreate(BaseModel):
    onchain_order_id: int
    fiat_amount: condecimal(gt=Decimal("0"), decimal_places=2)
    fiat_currency: str = Field(min_length=3, max_length=8)
    payment_method: str = Field(min_length=2, max_length=64)


class DisputeEvidenceCreate(BaseModel):
    order_id: int
    file_url: str
    note: str | None = None
