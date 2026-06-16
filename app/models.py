"""
models.py — Todos los modelos SQLAlchemy de la aplicación POS SRI Ecuador.

Tablas:
  - empresas         → datos del emisor (RUC, certificado, ambiente)
  - puntos_emision   → establecimientos y puntos de venta
  - clientes         → compradores (consumidor final o con RUC/CI)
  - productos        → catálogo de productos con precio e IVA
  - facturas         → cabecera de cada comprobante electrónico
  - detalles_factura → líneas de detalle de cada factura
"""

import enum
from sqlalchemy import (
    Column, Integer, String, Date, Numeric,
    Enum as SAEnum, ForeignKey, Text
)
from sqlalchemy.orm import relationship
from app.database import Base


# ══════════════════════════════════════════════════════════════════════════════
# Enumeraciones
# ══════════════════════════════════════════════════════════════════════════════

class EstadoSRI(str, enum.Enum):
    PENDIENTE     = "PENDIENTE"
    RECIBIDA      = "RECIBIDA"
    AUTORIZADA    = "AUTORIZADA"
    DEVUELTA      = "DEVUELTA"
    NO_AUTORIZADO = "NO_AUTORIZADO"


# ══════════════════════════════════════════════════════════════════════════════
# Emisor
# ══════════════════════════════════════════════════════════════════════════════

class Empresa(Base):
    """Datos del emisor de comprobantes electrónicos."""
    __tablename__ = "empresas"

    id                    = Column(Integer, primary_key=True, index=True)
    ruc                   = Column(String(13),  unique=True, index=True, nullable=False)
    razon_social          = Column(String(300), nullable=False)
    nombre_comercial      = Column(String(300), nullable=True)
    dir_matriz            = Column(String(500), nullable=False)

    # Certificado de firma digital — path al archivo .p12
    # En Render: usar variable de entorno CERT_P12_PATH
    certificado_p12_path     = Column(String(500), nullable=False)
    certificado_p12_password = Column(String(255), nullable=False)

    # Ambiente SRI: "1" = Pruebas, "2" = Producción
    ambiente = Column(String(1), default="1", nullable=False)

    # Relaciones
    puntos_emision = relationship("PuntoEmision", back_populates="empresa")
    facturas       = relationship("Factura", back_populates="empresa")


class PuntoEmision(Base):
    """Punto de venta / establecimientos del emisor."""
    __tablename__ = "puntos_emision"

    id               = Column(Integer, primary_key=True, index=True)
    empresa_id       = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    establecimiento  = Column(String(3), nullable=False, index=True)   # ej. "001"
    punto_emision    = Column(String(3), nullable=False, index=True)   # ej. "001"
    secuencial_actual = Column(Integer, default=0, nullable=False)     # auto-incremental

    # Relaciones
    empresa  = relationship("Empresa", back_populates="puntos_emision")
    facturas = relationship("Factura", back_populates="punto_emision")


# ══════════════════════════════════════════════════════════════════════════════
# Clientes
# ══════════════════════════════════════════════════════════════════════════════

class Cliente(Base):
    """Comprador / destinatario del comprobante."""
    __tablename__ = "clientes"

    id                  = Column(Integer, primary_key=True, index=True)
    ruc_ci              = Column(String(20),  unique=True, index=True, nullable=False)
    nombre              = Column(String(300), nullable=False)
    email               = Column(String(255), nullable=True)

    # Códigos SRI de tipo de identificación:
    # "04" = RUC, "05" = Cédula, "06" = Pasaporte, "07" = Consumidor Final,
    # "08" = Identificación del exterior
    tipo_identificacion = Column(String(2), default="07", nullable=False)

    # Relaciones
    facturas = relationship("Factura", back_populates="cliente")


# ══════════════════════════════════════════════════════════════════════════════
# Catálogo de Productos
# ══════════════════════════════════════════════════════════════════════════════

class Producto(Base):
    """Artículo o servicio vendible."""
    __tablename__ = "productos"

    id     = Column(Integer, primary_key=True, index=True)
    sku    = Column(String(50),  unique=True, index=True, nullable=False)
    nombre = Column(String(300), nullable=False)
    precio = Column(Numeric(10, 4), nullable=False)

    # Código de porcentaje IVA (catálogo SRI):
    # "0" = 0%, "2" = 12% (o 15% vigente), "6" = No Objeto de Impuesto
    tipo_iva = Column(String(1), default="2", nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# Comprobantes
# ══════════════════════════════════════════════════════════════════════════════

class Factura(Base):
    """Cabecera del comprobante de venta electrónico."""
    __tablename__ = "facturas"

    id               = Column(Integer, primary_key=True, index=True)
    empresa_id       = Column(Integer, ForeignKey("empresas.id"),       nullable=False)
    cliente_id       = Column(Integer, ForeignKey("clientes.id"),       nullable=False)
    punto_emision_id = Column(Integer, ForeignKey("puntos_emision.id"), nullable=False)

    # Identificadores SRI
    clave_acceso = Column(String(49), unique=True, index=True, nullable=False)
    secuencial   = Column(String(9),  nullable=False)
    fecha_emision = Column(Date,      nullable=False)
    estado_sri   = Column(SAEnum(EstadoSRI), default=EstadoSRI.PENDIENTE, nullable=False)

    # Almacenamiento del comprobante
    xml_path    = Column(String(500), nullable=True)           # Ruta al XML si se persiste en disco
    pdf_base64  = Column(Text,        nullable=True)           # RIDE PDF codificado en base64

    # Totales
    total_sin_impuestos = Column(Numeric(10, 2), nullable=False)
    iva_12              = Column(Numeric(10, 2), nullable=False)
    iva_0               = Column(Numeric(10, 2), nullable=False)
    total               = Column(Numeric(10, 2), nullable=False)

    # Relaciones
    detalles     = relationship("DetalleFactura", back_populates="factura",
                                cascade="all, delete-orphan")
    cliente      = relationship("Cliente",      back_populates="facturas")
    empresa      = relationship("Empresa",      back_populates="facturas")
    punto_emision = relationship("PuntoEmision", back_populates="facturas")


class DetalleFactura(Base):
    """Línea de producto dentro de una factura."""
    __tablename__ = "detalles_factura"

    id          = Column(Integer, primary_key=True, index=True)
    factura_id  = Column(Integer, ForeignKey("facturas.id"),  nullable=False)
    producto_id = Column(Integer, ForeignKey("productos.id"), nullable=False)

    cantidad        = Column(Numeric(10, 2), nullable=False)
    precio_unitario = Column(Numeric(10, 4), nullable=False)
    descuento       = Column(Numeric(10, 2), default=0.00, nullable=False)
    subtotal        = Column(Numeric(10, 2), nullable=False)  # (cantidad * precio) - descuento

    # Relaciones
    factura  = relationship("Factura",  back_populates="detalles")
    producto = relationship("Producto")
