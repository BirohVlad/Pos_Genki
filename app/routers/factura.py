from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import FacturarInput, FacturarResponse
from app.services.factura_service import factura_service

router = APIRouter(
    prefix="/facturar",
    tags=["Facturacion Electronica"]
)


@router.post(
    "",
    response_model=FacturarResponse,
    summary="Emitir y transmitir una factura electrónica al SRI",
    response_description="Respuesta del SRI con estado, clave de acceso y RIDE en base64"
)
def emitir_y_procesar_factura(
    payload: FacturarInput,
    db: Session = Depends(get_db)
):
    """
    Endpoint principal de facturación electrónica.

    **Flujo completo:**
    1. Bloquea el punto de emisión y obtiene el siguiente secuencial (control de concurrencia).
    2. Busca o crea al cliente en la base de datos.
    3. Valida que todos los SKUs existan en el catálogo de productos.
    4. Calcula subtotales, IVA (0% / 12%) y total de la factura.
    5. Persiste la factura en estado **PENDIENTE**.
    6. Genera el XML según el formato oficial del SRI v1.1.0.
    7. Firma el XML con **XAdES-BES** (Enveloped Signature) usando el certificado `.p12`.
    8. Envía el XML al servicio **validarComprobante** del SRI.
    9. Realiza **polling** al servicio **autorizacionComprobante** hasta obtener
       estado AUTORIZADO o agotar reintentos.
    10. Genera el **RIDE** (PDF) en base64 y lo retorna en la respuesta.

    **Errores posibles:**
    - `404` — Punto de emisión, empresa o producto no encontrado.
    - `400` — El SRI rechazó o devolvió el comprobante (detalle del error incluido).
    - `502` — Error de conectividad con los servicios SOAP del SRI.
    - `504` — El SRI no respondió la autorización en el tiempo máximo de espera.
    - `500` — Error interno inesperado.
    """
    return factura_service.emitir_factura(db=db, input_data=payload)
