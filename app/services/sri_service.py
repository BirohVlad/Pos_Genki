"""
sri_service.py — Integración completa con el SRI de Ecuador.

Métodos principales:
  generar_xml()        → Construye el XML de factura según el formato SRI v1.1.0
  firmar_xml()         → Firma con XAdES-BES (Enveloped Signature) usando .p12
  enviar_y_autorizar() → Envía al SRI vía SOAP y hace polling de autorización
  generar_ride_pdf()   → Genera el RIDE (PDF) en base64 usando ReportLab
"""

import base64
import datetime
import io
import os
import random
import time
from decimal import Decimal
from typing import Any, Dict, List, Tuple

import requests
from fastapi import HTTPException, status
from lxml import etree
from sri_xades_signer import sign_xml
from zeep import Client
from zeep.helpers import serialize_object
from zeep.transports import Transport

# ReportLab
from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)

# Suprimir warnings de SSL (certificados autofirmados del SRI en pruebas)
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════════════════════
# Funciones auxiliares para la clave de acceso
# ══════════════════════════════════════════════════════════════════════════════

def calcular_modulo11(clave_48: str) -> int:
    """Calcula el dígito verificador usando el algoritmo Módulo 11 del SRI."""
    if len(clave_48) != 48:
        raise ValueError(f"La clave debe tener 48 dígitos, tiene: {len(clave_48)}")
    multiplicadores = [2, 3, 4, 5, 6, 7]
    suma, idx = 0, 0
    for ch in reversed(clave_48):
        suma += int(ch) * multiplicadores[idx]
        idx = (idx + 1) % len(multiplicadores)
    residuo   = suma % 11
    verificador = 11 - residuo
    if verificador == 11:
        return 0
    if verificador == 10:
        return 1
    return verificador


def generar_clave_acceso(
    fecha_emision:    datetime.date,
    tipo_comprobante: str,
    ruc:              str,
    ambiente:         str,
    serie:            str,   # estab (3) + pto (3) = 6 dígitos
    secuencial:       str,   # 9 dígitos
    codigo_numerico:  str,   # 8 dígitos aleatorios
    tipo_emision:     str = "1"
) -> str:
    """Construye la clave de acceso de 49 dígitos (48 + dígito verificador)."""
    fecha_str = fecha_emision.strftime("%d%m%Y")
    clave_48 = (
        f"{fecha_str}"
        f"{str(tipo_comprobante).zfill(2)}"
        f"{str(ruc).zfill(13)}"
        f"{str(ambiente)}"
        f"{str(serie).zfill(6)}"
        f"{str(secuencial).zfill(9)}"
        f"{str(codigo_numerico).zfill(8)}"
        f"{str(tipo_emision)}"
    )
    return f"{clave_48}{calcular_modulo11(clave_48)}"


# ══════════════════════════════════════════════════════════════════════════════
# URLs de los servicios SOAP del SRI
# ══════════════════════════════════════════════════════════════════════════════

SRI_URLS = {
    "1": {  # Pruebas
        "recepcion":    "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/RecepcionComprobantesOffline?wsdl",
        "autorizacion": "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl",
    },
    "2": {  # Producción
        "recepcion":    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/RecepcionComprobantesOffline?wsdl",
        "autorizacion": "https://cel.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl",
    }
}


# ══════════════════════════════════════════════════════════════════════════════
# Servicio principal SRI
# ══════════════════════════════════════════════════════════════════════════════

class SRIService:
    """
    Encapsula la generación de XMLs, firma XAdES-BES,
    transmisión SOAP al SRI y renderizado del RIDE en PDF.
    """

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Generación del XML
    # ──────────────────────────────────────────────────────────────────────────

    def generar_xml(
        self,
        factura_data:      Any,
        detalles_data:     List[Any],
        empresa_data:      Any,
        punto_emision_data: Any
    ) -> Tuple[str, str]:
        """
        Genera el XML de factura electrónica (versión 1.1.0 SRI) usando lxml.etree
        y calcula la clave de acceso de 49 dígitos.

        Returns:
            Tuple[str, str]: (xml_string, clave_acceso)
        """
        fecha_emision     = factura_data.fecha_emision
        fecha_emision_str = fecha_emision.strftime("%d/%m/%Y")

        # 1a. Generar clave de acceso
        codigo_numerico = "".join(str(random.randint(0, 9)) for _ in range(8))
        serie = (
            f"{punto_emision_data.establecimiento.zfill(3)}"
            f"{punto_emision_data.punto_emision.zfill(3)}"
        )
        clave_acceso = generar_clave_acceso(
            fecha_emision    = fecha_emision,
            tipo_comprobante = "01",  # 01 = Factura
            ruc              = empresa_data.ruc,
            ambiente         = empresa_data.ambiente,
            serie            = serie,
            secuencial       = factura_data.secuencial,
            codigo_numerico  = codigo_numerico
        )
        factura_data.clave_acceso = clave_acceso  # Persistir en el objeto ORM

        # 1b. Agrupar bases imponibles por tipo de IVA
        bases_imponibles: Dict[str, Dict] = {}
        for d in detalles_data:
            t_iva = d.producto.tipo_iva
            if t_iva not in bases_imponibles:
                bases_imponibles[t_iva] = {"base": Decimal("0.00"), "valor": Decimal("0.00")}
            subtotal_item = Decimal(str(d.subtotal))
            bases_imponibles[t_iva]["base"] += subtotal_item
            if t_iva == "2":
                bases_imponibles[t_iva]["valor"] += (
                    subtotal_item * Decimal("0.12")
                ).quantize(Decimal("0.01"))

        # 1c. Construir árbol XML
        root = etree.Element("factura", id="comprobante", version="1.1.0")

        # <infoTributaria>
        info_trib = etree.SubElement(root, "infoTributaria")
        etree.SubElement(info_trib, "ambiente").text       = str(empresa_data.ambiente)
        etree.SubElement(info_trib, "tipoEmision").text    = "1"
        etree.SubElement(info_trib, "razonSocial").text    = empresa_data.razon_social
        if empresa_data.nombre_comercial:
            etree.SubElement(info_trib, "nombreComercial").text = empresa_data.nombre_comercial
        etree.SubElement(info_trib, "ruc").text            = empresa_data.ruc
        etree.SubElement(info_trib, "claveAcceso").text    = clave_acceso
        etree.SubElement(info_trib, "codDoc").text         = "01"
        etree.SubElement(info_trib, "estab").text          = punto_emision_data.establecimiento.zfill(3)
        etree.SubElement(info_trib, "ptoEmi").text         = punto_emision_data.punto_emision.zfill(3)
        etree.SubElement(info_trib, "secuencial").text     = factura_data.secuencial.zfill(9)
        etree.SubElement(info_trib, "dirMatriz").text      = empresa_data.dir_matriz

        # <infoFactura>
        info_fac = etree.SubElement(root, "infoFactura")
        etree.SubElement(info_fac, "fechaEmision").text               = fecha_emision_str
        etree.SubElement(info_fac, "dirEstablecimiento").text         = empresa_data.dir_matriz
        etree.SubElement(info_fac, "obligadoContabilidad").text       = "NO"
        etree.SubElement(info_fac, "tipoIdentificacionComprador").text = factura_data.cliente.tipo_identificacion
        etree.SubElement(info_fac, "razonSocialComprador").text       = factura_data.cliente.nombre
        etree.SubElement(info_fac, "identificacionComprador").text    = factura_data.cliente.ruc_ci
        etree.SubElement(info_fac, "totalSinImpuestos").text          = f"{factura_data.total_sin_impuestos:.2f}"

        total_desc = sum(Decimal(str(d.descuento)) for d in detalles_data)
        etree.SubElement(info_fac, "totalDescuento").text = f"{total_desc:.2f}"

        # <totalConImpuestos>
        total_impuestos = etree.SubElement(info_fac, "totalConImpuestos")
        for t_iva, vals in bases_imponibles.items():
            ti = etree.SubElement(total_impuestos, "totalImpuesto")
            etree.SubElement(ti, "codigo").text           = "2"  # 2 = IVA
            etree.SubElement(ti, "codigoPorcentaje").text = "2" if t_iva == "2" else "0"
            etree.SubElement(ti, "baseImponible").text    = f"{vals['base']:.2f}"
            etree.SubElement(ti, "tarifa").text           = "12.00" if t_iva == "2" else "0.00"
            etree.SubElement(ti, "valor").text            = f"{vals['valor']:.2f}"

        etree.SubElement(info_fac, "propina").text      = "0.00"
        etree.SubElement(info_fac, "importeTotal").text = f"{factura_data.total:.2f}"
        etree.SubElement(info_fac, "moneda").text       = "DOLAR"

        # <pagos>
        pagos = etree.SubElement(info_fac, "pagos")
        pago  = etree.SubElement(pagos, "pago")
        etree.SubElement(pago, "formaPago").text = "01"
        etree.SubElement(pago, "total").text     = f"{factura_data.total:.2f}"

        # <detalles>
        detalles_xml = etree.SubElement(root, "detalles")
        for d in detalles_data:
            det = etree.SubElement(detalles_xml, "detalle")
            etree.SubElement(det, "codigoPrincipal").text         = d.producto.sku
            etree.SubElement(det, "descripcion").text             = d.producto.nombre
            etree.SubElement(det, "cantidad").text                = f"{d.cantidad:.2f}"
            etree.SubElement(det, "precioUnitario").text          = f"{d.precio_unitario:.4f}"
            etree.SubElement(det, "descuento").text               = f"{d.descuento:.2f}"
            etree.SubElement(det, "precioTotalSinImpuesto").text  = f"{d.subtotal:.2f}"

            impuestos_xml = etree.SubElement(det, "impuestos")
            imp = etree.SubElement(impuestos_xml, "impuesto")
            etree.SubElement(imp, "codigo").text           = "2"
            etree.SubElement(imp, "codigoPorcentaje").text = "2" if d.producto.tipo_iva == "2" else "0"
            etree.SubElement(imp, "tarifa").text           = "12.00" if d.producto.tipo_iva == "2" else "0.00"
            etree.SubElement(imp, "baseImponible").text    = f"{d.subtotal:.2f}"
            val_imp = (
                (Decimal(str(d.subtotal)) * Decimal("0.12")).quantize(Decimal("0.01"))
                if d.producto.tipo_iva == "2" else Decimal("0.00")
            )
            etree.SubElement(imp, "valor").text = f"{val_imp:.2f}"

        xml_str = etree.tostring(
            root, pretty_print=True, encoding="utf-8", xml_declaration=True
        ).decode("utf-8")

        return xml_str, clave_acceso

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Firma XAdES-BES
    # ──────────────────────────────────────────────────────────────────────────

    def firmar_xml(self, xml_string: str, ruta_p12: str, password_p12: str) -> str:
        """
        Firma el XML usando sri-xades-signer (XAdES-BES Enveloped Signature).

        Args:
            xml_string:  XML sin firmar como string UTF-8.
            ruta_p12:    Ruta al archivo .p12 del certificado de firma.
            password_p12: Contraseña del .p12.

        Returns:
            XML firmado como string.
        """
        if not os.path.exists(ruta_p12):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Certificado no encontrado en la ruta configurada: {ruta_p12}"
            )
        try:
            xml_firmado = sign_xml(
                pkcs12_file = ruta_p12,
                password    = password_p12,
                xml         = xml_string,
                read_file   = True
            )
            return xml_firmado
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error en firma digital XAdES-BES: {str(exc)}"
            )

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Envío al SRI + Polling de autorización
    # ──────────────────────────────────────────────────────────────────────────

    def enviar_y_autorizar(
        self, xml_firmado: str, clave_acceso: str, ambiente: str
    ) -> Tuple[str, datetime.datetime, str]:
        """
        Envía el XML firmado al SRI (validarComprobante) y luego hace polling
        a autorizacionComprobante hasta obtener AUTORIZADO o agotar reintentos.

        Returns:
            Tuple[str, datetime, str]: (numero_autorizacion, fecha_autorizacion, xml_autorizado)
        """
        urls = SRI_URLS.get(str(ambiente), SRI_URLS["1"])
        url_recepcion    = urls["recepcion"]
        url_autorizacion = urls["autorizacion"]

        # Sesión requests con SSL deshabilitado para certificados del SRI
        session = requests.Session()
        session.verify = False

        # ── PASO 1: Recepción ──────────────────────────────────────────────
        xml_b64 = base64.b64encode(xml_firmado.encode("utf-8")).decode("utf-8")

        try:
            client_rec   = Client(url_recepcion, transport=Transport(session=session, timeout=30))
            response_rec = client_rec.service.validarComprobante(xml=xml_b64)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Error de conexión con el WS de Recepción del SRI: {str(exc)}"
            )

        data_rec   = serialize_object(response_rec)
        estado_rec = data_rec.get("estado")

        if estado_rec != "RECIBIDA":
            # Extraer mensajes de error estructurados del SRI
            comprobantes      = data_rec.get("comprobantes", {}) or {}
            comprobante_list  = comprobantes.get("comprobante") or []
            if not isinstance(comprobante_list, list):
                comprobante_list = [comprobante_list]

            mensajes_list = []
            for comp in comprobante_list:
                mensajes   = comp.get("mensajes", {}) or {}
                msg_items  = mensajes.get("mensaje") or []
                if not isinstance(msg_items, list):
                    msg_items = [msg_items]
                for m in msg_items:
                    mensajes_list.append(
                        f"[{m.get('tipo','ERROR')}] {m.get('identificador','')} "
                        f"- {m.get('mensaje','')} ({m.get('informacionAdicional','')})"
                    )

            error_detalle = "; ".join(mensajes_list) or "Error desconocido en recepción."
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El SRI devolvió estado '{estado_rec}'. Detalles: {error_detalle}"
            )

        # ── PASO 2: Polling de autorización ───────────────────────────────
        try:
            client_auth = Client(url_autorizacion, transport=Transport(session=session, timeout=30))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Error de conexión con el WS de Autorización del SRI: {str(exc)}"
            )

        MAX_REINTENTOS = 5
        ESPERA_SEGUNDOS = 3

        for intento in range(1, MAX_REINTENTOS + 1):
            time.sleep(ESPERA_SEGUNDOS)
            try:
                response_auth = client_auth.service.autorizacionComprobante(
                    claveAccesoComprobante=clave_acceso
                )
                data_auth = serialize_object(response_auth)

                auth_val = (data_auth.get("autorizaciones") or {}).get("autorizacion")
                if not auth_val:
                    continue  # Aún no disponible, reintentar

                auths = auth_val if isinstance(auth_val, list) else [auth_val]
                auth  = auths[0]
                estado_auth = auth.get("estado")

                if estado_auth == "AUTORIZADO":
                    return (
                        auth.get("numeroAutorizacion"),
                        auth.get("fechaAutorizacion"),
                        auth.get("comprobante")
                    )

                if estado_auth == "EN PROCESO":
                    continue  # Esperar más

                # RECHAZADO u otro estado final negativo
                mensajes  = auth.get("mensajes", {}) or {}
                msg_items = mensajes.get("mensaje") or []
                if not isinstance(msg_items, list):
                    msg_items = [msg_items]

                errores = [
                    f"[{m.get('tipo','ERROR')}] {m.get('identificador','')} - {m.get('mensaje','')}"
                    for m in msg_items
                ]
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Factura NO AUTORIZADA por el SRI. Detalles: {'; '.join(errores) or 'Sin detalles.'}"
                )

            except HTTPException:
                raise
            except Exception as exc:
                if intento == MAX_REINTENTOS:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Fallo persistente consultando autorización al SRI: {str(exc)}"
                    )

        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Factura recibida pero superó el tiempo máximo de autorización (EN PROCESO)."
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Generación del RIDE (PDF)
    # ──────────────────────────────────────────────────────────────────────────

    def generar_ride_pdf(
        self,
        factura_data:  Any,
        detalles_data: List[Any],
        clave_acceso:  str,
        estado_sri:    str,
        xml_autorizado: str = None
    ) -> str:
        """
        Genera el RIDE (Representación Impresa del Documento Electrónico) en PDF
        usando ReportLab, completamente en memoria, y retorna el base64.

        Returns:
            str: PDF codificado en Base64.
        """
        buffer = io.BytesIO()
        doc    = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=1.5*cm, rightMargin=1.5*cm,
            topMargin=1.5*cm,  bottomMargin=1.5*cm
        )

        PW     = 510   # Ancho útil de la página en puntos
        styles = getSampleStyleSheet()

        def estilo(name, font="Helvetica", size=8, color="#111827", align=0, **kw):
            return ParagraphStyle(
                name, parent=styles["Normal"],
                fontName=font, fontSize=size,
                textColor=colors.HexColor(color),
                alignment=align, **kw
            )

        title_s  = estilo("title",  "Helvetica-Bold",    11, "#0F172A", align=1, spaceAfter=5)
        val_s    = estilo("val",    "Helvetica",           8, "#111827")
        label_s  = estilo("lbl",   "Helvetica-Bold",       8, "#1e3a5f")
        head_s   = estilo("head",  "Helvetica-Bold",      7.5, "#1e3a5f")
        foot_s   = estilo("foot",  "Helvetica-Oblique",   6.5, "#6B7280", align=1)
        center_s = estilo("ctr",   "Helvetica",            8, "#111827", align=1)

        fecha_str  = factura_data.fecha_emision.strftime("%d/%m/%Y")
        serie_str  = (
            f"{factura_data.punto_emision.establecimiento.zfill(3)}-"
            f"{factura_data.punto_emision.punto_emision.zfill(3)}-"
            f"{factura_data.secuencial.zfill(9)}"
        )

        story = []

        # Título
        story.append(Paragraph(
            "REPRESENTACIÓN IMPRESA DE COMPROBANTE ELECTRÓNICO (RIDE)", title_s
        ))

        GRID_STYLE = [
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("BOX",           (0, 0), (-1, -1), 0.5,  colors.HexColor("#9CA3AF")),
            ("LINEBEFORE",    (1, 0), (1,  -1), 0.5,  colors.HexColor("#9CA3AF")),
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ]

        # Cabecera: Emisor | Factura
        estado_color = "#15803d" if estado_sri == "AUTORIZADA" else "#b91c1c"
        fecha_auth   = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        emisor_p = Paragraph(
            f"<b>RUC:</b> {factura_data.empresa.ruc}<br/>"
            f"<b>RAZÓN SOCIAL:</b> {factura_data.empresa.razon_social}<br/>"
            f"<b>Dirección Matriz:</b> {factura_data.empresa.dir_matriz}<br/>"
            f"<b>Obligado Contabilidad:</b> NO",
            val_s
        )
        comp_p = Paragraph(
            f'<font size="11"><b>FACTURA</b></font><br/>'
            f"<b>No.:</b> {serie_str}<br/>"
            f"<b>Nro. Autorización:</b><br/>"
            f'<font size="6.5">{clave_acceso}</font><br/>'
            f"<b>Fecha Autorización:</b> {fecha_auth}<br/>"
            f"<b>Ambiente:</b> {'PRUEBAS' if factura_data.empresa.ambiente == '1' else 'PRODUCCIÓN'}<br/>"
            f'<b>Estado:</b> <font color="{estado_color}"><b>{estado_sri}</b></font>',
            val_s
        )

        hdr = Table([[emisor_p, comp_p]], colWidths=[PW * 0.55, PW * 0.45])
        hdr.setStyle(TableStyle(GRID_STYLE))
        story.append(hdr)
        story.append(Spacer(1, 8))

        # Código de barras Code128
        try:
            bc = Code128(clave_acceso, barWidth=0.85, barHeight=50)
            bc.hAlign = "CENTER"
            story.append(bc)
        except Exception:
            story.append(Paragraph("[Código de barras no disponible]", center_s))

        story.append(Spacer(1, 3))
        story.append(Paragraph("<b>Clave de Acceso:</b>", center_s))
        story.append(Paragraph(f'<font name="Courier" size="7.5">{clave_acceso}</font>', center_s))
        story.append(Spacer(1, 8))

        # Datos del comprador
        cli_p = Paragraph(
            f"<b>Razón Social / Cliente:</b> {factura_data.cliente.nombre} &nbsp;&nbsp;"
            f"<b>Identificación:</b> {factura_data.cliente.ruc_ci} &nbsp;&nbsp;"
            f"<b>Fecha Emisión:</b> {fecha_str}",
            val_s
        )
        cli_tbl = Table([[cli_p]], colWidths=[PW])
        cli_tbl.setStyle(TableStyle([
            ("BOX",        (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
            ("PADDING",    (0, 0), (-1, -1), 7),
        ]))
        story.append(cli_tbl)
        story.append(Spacer(1, 8))

        # Tabla de detalles
        det_data = [[
            Paragraph("<b>Cód.</b>",        head_s),
            Paragraph("<b>Descripción</b>", head_s),
            Paragraph("<b>Cant.</b>",        head_s),
            Paragraph("<b>P. Unitario</b>", head_s),
            Paragraph("<b>Subtotal</b>",     head_s),
        ]]
        for d in detalles_data:
            det_data.append([
                Paragraph(d.producto.sku,            val_s),
                Paragraph(d.producto.nombre,          val_s),
                Paragraph(f"{d.cantidad:.2f}",        val_s),
                Paragraph(f"{d.precio_unitario:.2f}", val_s),
                Paragraph(f"{d.subtotal:.2f}",        val_s),
            ])

        det_tbl = Table(det_data, colWidths=[60, 255, 45, 75, 75])
        det_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0),  colors.HexColor("#DBEAFE")),
            ("BOX",        (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
            ("INNERGRID",  (0, 0), (-1, -1), 0.3, colors.HexColor("#D1D5DB")),
            ("ALIGN",      (2, 0), (-1, -1), "RIGHT"),
            ("PADDING",    (0, 0), (-1, -1), 5),
            ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(det_tbl)
        story.append(Spacer(1, 8))

        # Totales
        base_iva_12  = factura_data.total_sin_impuestos - factura_data.iva_0
        desc_total   = sum(Decimal(str(d.descuento)) for d in detalles_data)

        totales = [
            [Paragraph("<b>SUBTOTAL IVA 0%</b>",  label_s), Paragraph(f"{factura_data.iva_0:.2f}",    val_s)],
            [Paragraph("<b>SUBTOTAL IVA 12%</b>", label_s), Paragraph(f"{base_iva_12:.2f}",            val_s)],
            [Paragraph("<b>DESCUENTO</b>",         label_s), Paragraph(f"{desc_total:.2f}",             val_s)],
            [Paragraph("<b>IVA 12%</b>",           label_s), Paragraph(f"{factura_data.iva_12:.2f}",   val_s)],
            [Paragraph("<b>VALOR TOTAL</b>",       label_s), Paragraph(f"$ {factura_data.total:.2f}",  val_s)],
        ]
        tot_tbl = Table(totales, colWidths=[130, 70])
        tot_tbl.setStyle(TableStyle([
            ("BOX",        (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
            ("INNERGRID",  (0, 0), (-1, -1), 0.3, colors.HexColor("#D1D5DB")),
            ("BACKGROUND", (0, 0), (-1, -2), colors.HexColor("#F9FAFB")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DBEAFE")),
            ("ALIGN",      (1, 0), (1, -1),  "RIGHT"),
            ("PADDING",    (0, 0), (-1, -1), 5),
        ]))

        layout = Table([["", tot_tbl]], colWidths=[PW - 200, 200])
        layout.setStyle(TableStyle([
            ("VALIGN",  (0, 0), (-1, -1), "TOP"),
            ("PADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(layout)
        story.append(Spacer(1, 10))

        # Pie
        story.append(HRFlowable(width=PW, thickness=0.5, color=colors.HexColor("#9CA3AF")))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "Comprobante generado por sistema de facturación electrónica — "
            f"Ambiente {'de Pruebas' if factura_data.empresa.ambiente == '1' else 'de Producción'} SRI Ecuador",
            foot_s
        ))

        doc.build(story)
        pdf_data = buffer.getvalue()
        buffer.close()

        return base64.b64encode(pdf_data).decode("utf-8")


# Instancia singleton del servicio
sri_service = SRIService()
