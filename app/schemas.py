"""
schemas.py — Modelos Pydantic para validación de entrada/salida de la API.
"""

from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
from decimal import Decimal
import datetime


# ══════════════════════════════════════════════════════════════════════════════
# Schemas de entrada (Input / Request)
# ══════════════════════════════════════════════════════════════════════════════

class DetalleFacturaInput(BaseModel):
    """Un producto dentro de la orden de venta."""
    sku: str = Field(
        ...,
        description="SKU del producto registrado en el catálogo",
        examples=["PROD-001"]
    )
    cantidad: Decimal = Field(
        ...,
        gt=0,
        description="Cantidad a facturar (debe ser mayor a 0)",
        examples=[2]
    )
    descuento: Decimal = Field(
        Decimal("0.00"),
        ge=0,
        description="Descuento monetario aplicado al subtotal de este ítem",
        examples=[0.00]
    )


class FacturarInput(BaseModel):
    """
    Cuerpo completo de la petición POST /api/facturar.
    Contiene los datos del comprador, el punto de emisión y los productos.
    """
    # ── Comprador ──────────────────────────────────────────────────────────────
    cliente_ruc: str = Field(
        ...,
        min_length=10,
        max_length=13,
        description="RUC (13 dígitos), Cédula (10 dígitos) o '9999999999999' para Consumidor Final",
        examples=["9999999999999"]
    )
    cliente_nombre: str = Field(
        ...,
        max_length=300,
        description="Razón Social o nombre completo del comprador",
        examples=["CONSUMIDOR FINAL"]
    )
    cliente_email: Optional[EmailStr] = Field(
        None,
        description="Correo electrónico del comprador (opcional)"
    )
    tipo_identificacion: str = Field(
        "07",
        description=(
            "Código SRI del tipo de identificación: "
            "'04'=RUC, '05'=Cédula, '06'=Pasaporte, "
            "'07'=Consumidor Final, '08'=Id. del Exterior"
        ),
        examples=["07"]
    )

    # ── Punto de emisión ──────────────────────────────────────────────────────
    establecimiento: str = Field(
        "001",
        min_length=3,
        max_length=3,
        description="Código del establecimiento (3 dígitos)",
        examples=["001"]
    )
    punto_emision: str = Field(
        "001",
        min_length=3,
        max_length=3,
        description="Código del punto de emisión (3 dígitos)",
        examples=["001"]
    )

    # ── Pago ──────────────────────────────────────────────────────────────────
    tipo_pago: str = Field(
        "01",
        description=(
            "Código SRI de forma de pago: "
            "'01'=Sin sistema financiero (efectivo), '16'=Tarjeta de débito, "
            "'19'=Tarjeta de crédito, '20'=Otros con utilización del sistema financiero"
        ),
        examples=["01"]
    )

    # ── Productos ─────────────────────────────────────────────────────────────
    productos: List[DetalleFacturaInput] = Field(
        ...,
        min_length=1,
        description="Lista de productos/servicios a incluir en la factura (mínimo 1)"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Schemas de salida (Output / Response)
# ══════════════════════════════════════════════════════════════════════════════

class FacturarResponse(BaseModel):
    """Respuesta del endpoint POST /api/facturar."""
    estado: str = Field(
        ...,
        description="Estado final del comprobante: AUTORIZADA, NO_AUTORIZADO, DEVUELTA, etc.",
        examples=["AUTORIZADA"]
    )
    clave_acceso: str = Field(
        ...,
        description="Clave de acceso de 49 dígitos asignada por el SRI",
        examples=["1612202501060442690800110010010000000011234567891"]
    )
    pdf_base64: Optional[str] = Field(
        None,
        description="RIDE en formato PDF codificado en Base64 (solo si fue autorizado)"
    )
    mensaje: Optional[str] = Field(
        None,
        description="Información adicional sobre el resultado de la transacción"
    )

    class Config:
        from_attributes = True   # Pydantic v2 (reemplaza orm_mode = True)


# ══════════════════════════════════════════════════════════════════════════════
# Schemas informativos (para endpoints de consulta futura)
# ══════════════════════════════════════════════════════════════════════════════

class DetalleFacturaOut(BaseModel):
    id: int
    sku: Optional[str] = None
    nombre_producto: Optional[str] = None
    cantidad: Decimal
    precio_unitario: Decimal
    descuento: Decimal
    subtotal: Decimal

    class Config:
        from_attributes = True


class FacturaOut(BaseModel):
    id: int
    clave_acceso: str
    secuencial: str
    fecha_emision: datetime.date
    estado_sri: str
    total_sin_impuestos: Decimal
    iva_12: Decimal
    iva_0: Decimal
    total: Decimal
    cliente_nombre: Optional[str] = None
    detalles: List[DetalleFacturaOut] = []

    class Config:
        from_attributes = True
