"""
factura_service.py — Lógica transaccional de facturación.

Responsabilidades:
  - Bloqueo pesimista del secuencial (FOR UPDATE) para evitar duplicados concurrentes.
  - Búsqueda/creación del cliente.
  - Cálculo de subtotales, IVA y totales.
  - Persistencia de Factura en estado PENDIENTE.
  - Orquestación del flujo SRI: generar_xml → firmar → enviar_y_autorizar → generar_ride.
"""

import datetime
from decimal import Decimal
from typing import List

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import (
    Empresa, PuntoEmision, Cliente, Producto,
    Factura, DetalleFactura, EstadoSRI
)
from app.schemas import FacturarInput, FacturarResponse
from app.services.sri_service import sri_service


class FacturaService:
    """
    Orquesta la emisión completa de una factura electrónica:
    BD → XML → Firma → SRI → PDF RIDE.
    """

    def emitir_factura(self, db: Session, input_data: FacturarInput) -> FacturarResponse:
        """
        Flujo principal de facturación. Lanza HTTPException en cualquier error
        para que FastAPI devuelva una respuesta JSON clara al cliente Flutter.
        """

        # ── 1. Bloquear y obtener el siguiente secuencial ───────────────────
        secuencial_int = self._obtener_y_bloquear_secuencial(
            db, input_data.establecimiento, input_data.punto_emision
        )
        secuencial_str = str(secuencial_int).zfill(9)

        # ── 2. Obtener el punto de emisión y la empresa ─────────────────────
        punto = db.query(PuntoEmision).filter(
            PuntoEmision.establecimiento == input_data.establecimiento,
            PuntoEmision.punto_emision   == input_data.punto_emision
        ).first()

        if not punto:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Punto de emisión {input_data.establecimiento}-{input_data.punto_emision} no encontrado."
            )

        empresa = punto.empresa
        if not empresa:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Empresa asociada al punto de emisión no encontrada."
            )

        # ── 3. Buscar o crear el cliente ─────────────────────────────────────
        cliente = db.query(Cliente).filter(Cliente.ruc_ci == input_data.cliente_ruc).first()
        if not cliente:
            cliente = Cliente(
                ruc_ci              = input_data.cliente_ruc,
                nombre              = input_data.cliente_nombre,
                email               = input_data.cliente_email,
                tipo_identificacion = input_data.tipo_identificacion
            )
            db.add(cliente)
            db.commit()
            db.refresh(cliente)

        # ── 4. Catálogo de productos y totales ───────────────────────────────
        detalles_factura: List[DetalleFactura] = []
        subtotal_iva_0  = Decimal("0.00")
        subtotal_iva_12 = Decimal("0.00")

        for item in input_data.productos:
            producto = db.query(Producto).filter(Producto.sku == item.sku).first()
            if not producto:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Producto con SKU '{item.sku}' no existe en el catálogo."
                )

            cantidad_dec  = Decimal(str(item.cantidad))
            precio_dec    = Decimal(str(producto.precio))
            descuento_dec = Decimal(str(item.descuento))

            subtotal_item = (cantidad_dec * precio_dec) - descuento_dec
            if subtotal_item < 0:
                subtotal_item = Decimal("0.00")

            if producto.tipo_iva == "2":
                subtotal_iva_12 += subtotal_item
            else:
                subtotal_iva_0 += subtotal_item

            detalles_factura.append(
                DetalleFactura(
                    producto_id     = producto.id,
                    cantidad        = cantidad_dec,
                    precio_unitario = precio_dec,
                    descuento       = descuento_dec,
                    subtotal        = subtotal_item
                )
            )

        # Totales generales
        iva_12_total = (subtotal_iva_12 * Decimal("0.12")).quantize(Decimal("0.01"))
        total_final  = (subtotal_iva_0 + subtotal_iva_12 + iva_12_total).quantize(Decimal("0.01"))

        # ── 5. Persistir factura en estado PENDIENTE ─────────────────────────
        factura = Factura(
            empresa_id          = empresa.id,
            cliente_id          = cliente.id,
            punto_emision_id    = punto.id,
            clave_acceso        = "",
            secuencial          = secuencial_str,
            fecha_emision       = datetime.date.today(),
            estado_sri          = EstadoSRI.PENDIENTE,
            total_sin_impuestos = subtotal_iva_0 + subtotal_iva_12,
            iva_12              = iva_12_total,
            iva_0               = subtotal_iva_0,
            total               = total_final
        )
        factura.detalles = detalles_factura

        db.add(factura)
        db.commit()
        db.refresh(factura)

        # ── 6. Flujo de integración con el SRI ──────────────────────────────
        try:
            # A. Generar XML (asigna clave_acceso)
            xml_str, clave_acceso = sri_service.generar_xml(
                factura, factura.detalles, empresa, punto
            )
            db.commit()

            # B. Firmar XML
            xml_firmado = sri_service.firmar_xml(
                xml_str,
                empresa.certificado_p12_path,
                empresa.certificado_p12_password
            )

            # C. Transmitir y Autorizar
            try:
                num_auth, fecha_auth, xml_auth = sri_service.enviar_y_autorizar(
                    xml_firmado, clave_acceso, empresa.ambiente
                )
                factura.estado_sri = EstadoSRI.AUTORIZADA
                db.commit()

            except HTTPException as http_exc:
                detail = str(http_exc.detail)
                if "DEVUELTA" in detail:
                    factura.estado_sri = EstadoSRI.DEVUELTA
                else:
                    factura.estado_sri = EstadoSRI.NO_AUTORIZADO
                db.commit()
                raise http_exc

            # D. Generar PDF RIDE
            pdf_b64 = sri_service.generar_ride_pdf(
                factura, factura.detalles, clave_acceso, "AUTORIZADA", xml_auth
            )
            factura.pdf_base64 = pdf_b64
            db.commit()

            return FacturarResponse(
                estado       = EstadoSRI.AUTORIZADA.value,
                clave_acceso = clave_acceso,
                pdf_base64   = pdf_b64,
                mensaje      = "Factura autorizada exitosamente por el SRI."
            )

        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error inesperado procesando el comprobante: {str(exc)}"
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Bloqueo pesimista del secuencial
    # ──────────────────────────────────────────────────────────────────────────
    def _obtener_y_bloquear_secuencial(
        self, db: Session, establecimiento: str, punto_emision: str
    ) -> int:
        try:
            is_sqlite = "sqlite" in str(db.get_bind().url)
            query = db.query(PuntoEmision).filter(
                PuntoEmision.establecimiento == establecimiento,
                PuntoEmision.punto_emision   == punto_emision
            )

            if not is_sqlite:
                query = query.with_for_update()  # Bloqueo SELECT FOR UPDATE en Postgres

            punto = query.first()

            if not punto:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"Establecimiento '{establecimiento}' o punto de emisión "
                        f"'{punto_emision}' no configurado."
                    )
                )

            nuevo_secuencial        = punto.secuencial_actual + 1
            punto.secuencial_actual = nuevo_secuencial

            db.commit()
            return nuevo_secuencial

        except HTTPException:
            raise
        except Exception as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error al generar el secuencial del comprobante: {str(exc)}"
            )


factura_service = FacturaService()
