# Contexto del dominio para los informes IA — BORRADOR para revisar

> **Estado: borrador del 2026-10-28, armado con lo que hay en Asana y en el código. TODAVÍA NO
> SE USA en el prompt.** Antes de conectarlo hay que revisar cada sección y contestar las
> preguntas del final. Lo marcado con **(confirmar)** es una inferencia, no un dato verificado.
> Este archivo no debe tener datos personales, credenciales ni nombres de pacientes.

## 1. Quiénes somos

Tecnoimagen (unidad Medical IT) implementa y da soporte a la plataforma **Suitestensa** (SE),
de Ebit/Esaote, en hospitales públicos y privados de Argentina. La plataforma incluye:

- **RIS**: gestión del servicio de diagnóstico por imágenes (admisión, agenda, asignación de
  equipos, informes).
- **PACS**: almacenamiento y distribución de imágenes médicas (DICOM).
- **CVIS**: módulo de cardiología (hemodinamia, ecocardiografía, ECG).
- **Visores**: Eunity (web) y Univiewer; servicios 3D.
- **Dictado por voz** para los informes (Dycendo **(confirmar)**).
- **Portal paciente**: los pacientes consultan sus estudios e informes por web o código QR.
- **VNA / LongTerm**: archivo de largo plazo.

Proyectos y redes de hospitales:
- **BID**: hospitales H01–H46, con servidores Dell PowerEdge R7525 y mantenimiento preventivo
  (actualización de firmware).
- **PROSEPU II**: hospitales P01–P26.
- **PAMI**: policlínicos y hospitales PM* (Milstein, Houssay, Ituzaingó, Policlínicos I y II).
- **Privados y obras sociales**: HUPC, Sanatorio de la Mujer, Hospital Español de Mar del Plata,
  ICBA, OSECAC.

## 2. Cómo se da el soporte

- **CSAC**, el centro de soporte, recibe los casos por un formulario. Cada caso indica:
  - tipo (incidencia o fallo, pedido, capacitación);
  - criticidad: Crítico = cierre de servicio o pérdida de datos inmediata; Alto = afecta a muchos
    usuarios o a una función clave;
  - plataforma afectada: web, infraestructura o toda la plataforma.
- **Niveles:** Tecnoimagen es el primer nivel. Los casos complejos se escalan a **L2/L3** (casa
  matriz Ebit/Esaote) y pueden llevar semanas, porque dependen de terceros y de ventanas de
  mantenimiento acordadas con el hospital.
- **Éxito del cliente:** seguimiento trimestral por provincia y hospital del **uso real** de la
  plataforma (RIS/PACS/CVIS). Mira adopción, si cada equipo hace el flujo completo,
  capacitación, puntos de dolor y riesgos tempranos, y define próximos pasos.

## 3. Qué es TecnoMonitor

- **Herramienta interna y preventiva** del equipo. A futuro se contempla un acceso de solo
  visualización para los clientes.
- **Agente:** se instala en una VM de cada hospital y reporta cada **5 minutos** sobre:
  - el host físico (Proxmox o VMware, más el iDRAC de Dell: temperaturas, fuentes, ventiladores,
    RAID y discos);
  - las VMs Windows (CPU, RAM, discos, servicios);
  - Mirth Connect (canales HL7);
  - Elasticsearch (logs de Suitestensa);
  - SQL Server (backups, integridad con CHECKDB).
- **Datos más lentos:** los KPIs de RIS/PACS y la cantidad de imágenes llegan con menos
  frecuencia: por bloques de horas o una vez por día.
- **Motor de alertas:** usa umbrales globales y tiene reglas de exclusión para falsos positivos
  conocidos, como discos que trabajan casi llenos a propósito. Abre una tarea en Asana por
  alerta, la reabre si el problema vuelve y la cierra sola cuando el valor se normaliza.
- **TecnoMonitor detecta y avisa; no repara.** La acción la toma el equipo de soporte. A veces
  hace falta el hospital (su área de sistemas, bioingeniería) o L2.

## 4. Infraestructura típica de un hospital

- **Host físico:** servidor Dell PowerEdge, con iDRAC para el hardware y Proxmox como
  hipervisor. Algunos sitios usan VMware.
- **VMs, por sufijo del nombre** (ej. `ARP19APPV` = Argentina, P19, APPV):

  | Sufijo | Qué es |
  |---|---|
  | `APPV` | Servidor de aplicaciones: SQL Server con las bases de Suitestensa y los servicios de RIS/PACS. Es la VM principal. |
  | `EU0V` | Servidor del visor Eunity. Lleva certificado SSL propio. |
  | `PRTV` | VM del portal paciente **(confirmar)** |
  | `VOCV` | Dictado por voz **(confirmar)** |
  | `SQLV` | SQL Server separado; en algunos PAMI **(confirmar)** |

- **Bases SQL de Suitestensa:** ExtensaRadio (RIS), ExtensaPACS, ExtensaCardio,
  ExtensaPatient, ExtensaMPS (portal), SL_UserAndConfig (usuarios), entre otras.
- **Modalidades** (equipos que generan estudios): CR/DX (radiología), CT (tomografía), MR
  (resonancia), US (ecografía), MG (mamografía), entre otras.

## 5. Flujo clínico (para entender el impacto de cada problema)

1. **Admisión:** el paciente se admite en el HIS del hospital y la orden llega al RIS por **HL7**
   (mensajes ADT/ORM), o se carga a mano en el RIS.
2. **Estudio:** el técnico lo realiza en la modalidad, que toma los datos del paciente de la
   lista de trabajo.
3. **Imágenes:** se envían al PACS por **DICOM**. Algunas reglas de **autoenrute** las copian a
   otros nodos (cachés, centrales, PACS paralelos).
4. **Visualización:** los médicos ven las imágenes en Eunity o Univiewer, en salas de informe y
   en los pisos.
5. **Informe:** el médico informante dicta o escribe el informe (borrador) y lo firma
   (definitivo).
6. **Entrega:**
   - el informe vuelve al HIS por HL7 (mensaje **ORU**);
   - el portal paciente publica estudio e informe (el servicio MPS arma lo que se publica) y el
     paciente accede con usuario o QR.

## 6. Integraciones HL7 (Mirth Connect)

- **Motor:** Mirth Connect traduce y transporta los mensajes HL7 v2.x entre el HIS del hospital
  y Suitestensa. Cada canal es un flujo: admisiones que entran, resultados que salen, etc.
- **HIS integrados** (según los proyectos de Asana): Tasy (H. Posadas), HSI (sistema nacional),
  Markis (Tierra del Fuego), SISGH (PAMI), SAFESA (Salta), INNOVA (P10), ANDES (Neuquén),
  SIPROSA (Tucumán), EGeS, Pegasus (IMAC Salta), GMS (OSECAC).
- **Si un canal no funciona:**
  - las admisiones no llegan al RIS y hay que cargar pacientes a mano; pasó, por ejemplo, un
    paciente cargado a mano en el tomógrafo, que después no pudo pedir su informe;
  - o los informes no vuelven al HIS.
- **Impacto:** es un problema **operativo directo** para el servicio.
- **Ojo:** muchas fallas de integración dependen del otro sistema (el HIS), no de Suitestensa.
  No hay que atribuir la causa sin datos.

## 7. Qué significa cada señal del informe

| Señal | Qué significa para el hospital | Precaución |
|---|---|---|
| Disponibilidad (% del período reportando) | Si los sistemas estuvieron en línea | Un hueco puede ser una caída real **o** el agente sin enviar datos; no afirmar cuál sin otra evidencia |
| VM fuera de línea / reinicio | Servicios de esa VM interrumpidos (RIS/PACS si es la APPV) | Un reinicio puede ser programado (mantenimiento, actualizaciones) |
| Servicio de Windows detenido | Una función de Suitestensa sin funcionar | Algunos servicios no se usan en todos los hospitales |
| RAM de VM alta | Posible lentitud | En las APPV, el SQL Server usa casi toda la RAM a propósito, y hay una hipótesis de "ballooning" de Proxmox: **un 85–90 % sostenido no es, por sí solo, una falla** |
| Disco de VM lleno | Riesgo de que se detenga la escritura (imágenes, bases) | Algunos discos trabajan llenos a propósito y tienen exclusión |
| Fuente de energía / ventilador / RAID / temperatura | Riesgo de hardware físico | Una fuente redundante en 0 W con estado OK está en reposo, no falla |
| Canal Mirth con error / cola | Integración HL7 interrumpida o atrasada (ver §6) | La causa puede estar del lado del HIS |
| Cola de autoenrute DICOM | Imágenes esperando copiarse a otro nodo | Colas transitorias son normales; importa si crecen sostenidamente |
| Portal paciente (estados de publicación) | Estudios que el paciente todavía no puede ver | — |
| Backups SQL | Riesgo de pérdida de datos | **La replicación o el Disaster Recovery no reemplazan un backup** (no protegen de un borrado o de ransomware) |
| CHECKDB con errores | Posible corrupción de una base | Se ejecuta solo después de un reinicio de SQL |
| Certificados SSL por vencer | Acceso web (visor, portal) bloqueado al vencer | — |
| KPIs de RIS/PACS (volúmenes) | Uso real del sistema | Un volumen bajo puede ser **baja adopción** (el servicio no usa el sistema) y no baja actividad del hospital |
| Módulo sin datos | — | Puede ser un módulo no instalado en ese hospital: **no es "sin problemas" ni "falla"** |

## 8. Qué le importa al cliente (dirección del hospital, ministerio, obra social)

Sale de los seguimientos de Éxito del cliente y de los resúmenes ejecutivos de Asana.

- Que el sistema esté disponible y que el servicio funcione con fluidez.
- **Adopción:** que los servicios y equipos usen el flujo completo (admisión, estudio, imagen,
  informe, entrega), con capacitación y acompañamiento.
- Continuidad: resguardo de datos, garantía, crecimiento del almacenamiento.
- Tono de referencia (resumen ejecutivo real de un proyecto): un balance general primero; los
  desafíos como "desafíos" u "oportunidades", con propuesta de acción; foco en adopción y
  planificación.

## 9. Preguntas pendientes (contestar antes de conectar este archivo)

1. Sufijos de VM: confirmar `PRTV`, `VOCV`, `SQLV` y agregar los que falten.
2. Ante un incidente, ¿qué hace exactamente TecnoMonitor o el equipo (solo avisa, coordina con
   Ebit/Esaote, interviene)? ¿Qué no le corresponde (infraestructura de red del hospital,
   equipos de las modalidades, el HIS)?
3. ¿El informe para el cliente habla de "Tecnoimagen", de "TecnoMonitor" o de "el servicio"?
4. ¿Hay compromisos de servicio (SLA, garantía) que se puedan o no se deban mencionar?
5. ¿Otros falsos positivos conocidos?
6. ¿Algo del flujo clínico (§5) que no sea así en todos los hospitales?
