import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
import concurrent.futures

st.set_page_config(page_title="HY-NET | Escáner de Bypass y Medición", page_icon="🚨", layout="wide")
st.title("🚨 HY-NET | Escáner Regional de Bypass y Medición")
st.caption("Analiza masivamente el estado de Bypass, detección de fallas de medición y caídas recientes (Data Gaps).")

# --- FUNCIONES DE EXTRACCIÓN Y SUMA ---
def obtener_multiplicador(data_dict, parametro):
    try:
        unidad = data_dict.get('meta', {}).get('parameterUnits', {}).get(parametro, '').lower()
        return 1000 if unidad == 'kw' else 1
    except:
        return 1

# Extrae la suma de los datos Y el tiempo de la última actualización
def extraer_datos_y_tiempo(data_dict, clave_buscada):
    items = data_dict.get('data', [])
    suma_total = 0.0
    dato_encontrado = False
    tiempos = []
    
    if items and isinstance(items, list):
        for item in items:
            inner_data = item.get('data', {}) 
            if clave_buscada in inner_data:
                val = inner_data[clave_buscada]
                
                num = None
                if isinstance(val, dict):
                    num = val.get('last', val.get('mean'))
                    t_str = val.get('time')
                    if t_str: tiempos.append(t_str)
                else:
                    num = val
                
                if num is not None:
                    try:
                        suma_total += float(num)
                        dato_encontrado = True
                    except:
                        pass
                        
    tiempo_reciente = None
    if tiempos:
        tiempos_dt = []
        for t in tiempos:
            try:
                dt = datetime.fromisoformat(t.replace('Z', '+00:00'))
                tiempos_dt.append(dt)
            except:
                pass
        if tiempos_dt:
            tiempo_reciente = max(tiempos_dt)

    return (suma_total if dato_encontrado else None), tiempo_reciente

# --- MOTOR DE ESCANEO MASIVO ---
def escanear_sitio(sitio, session, start_sec, end_sec, ahora_utc):
    ps_id = sitio["SITIO_ID"]
    url_inversor = f"https://api.stellar.newsunroad.com/api/v0/devices/{ps_id}?end={end_sec}&start={start_sec}&params=inverterLoadPower&deviceClass=Inverter"
    url_medidor = f"https://api.stellar.newsunroad.com/api/v0/devices/{ps_id}?end={end_sec}&start={start_sec}&params=acLoadPower&deviceClass=LoadMeter"
    
    try:
        res_inv = session.get(url_inversor, timeout=10)
        res_med = session.get(url_medidor, timeout=10)
        
        data_inv = res_inv.json() if res_inv.status_code == 200 else {}
        data_med = res_med.json() if res_med.status_code == 200 else {}
        
        raw_inv, tiempo_inv = extraer_datos_y_tiempo(data_inv, 'inverterLoadPower')
        mult_inv = obtener_multiplicador(data_inv, 'inverterLoadPower')
        load_power = (raw_inv * mult_inv) if raw_inv is not None else None

        raw_med, tiempo_med = extraer_datos_y_tiempo(data_med, 'acLoadPower')
        mult_med = obtener_multiplicador(data_med, 'acLoadPower')
        met_power = (raw_med * mult_med) if raw_med is not None else None
        
        # Validar Data Gap (Si los datos tienen más de 30 minutos de antigüedad, es un falso positivo)
        datos_viejos = False
        tiempo_evaluar = tiempo_inv or tiempo_med
        if tiempo_evaluar:
            diferencia_minutos = (ahora_utc - tiempo_evaluar).total_seconds() / 60
            if diferencia_minutos > 30:
                datos_viejos = True

        # === LÓGICA DE CLASIFICACIÓN ===
        if (load_power is None and met_power is None) or datos_viejos:
            estado = "Sin Gestión"
            indicador = "🔴 Offline / Data Gap"
            # Borramos la lectura visual porque los datos están congelados
            load_power = None
            met_power = None
        elif load_power is not None:
            medidor_inactivo = (met_power is None or met_power <= 100)
            
            if load_power < 500 and medidor_inactivo:
                estado = "Bypass"
                indicador = "⚠️ Bypass"
            elif met_power is not None and met_power > 0:
                # Evaluación de proporción entre potencia del inversor y potencia del medidor
                relacion = load_power / met_power
                if relacion < 0.75 or relacion > 1.25:
                    estado = "Falla Medición"
                    indicador = "📐 Falla Medición"
                else:
                    estado = "Normal"
                    indicador = "✅ Normal"
            elif medidor_inactivo:
                estado = "Normal (Sin Medidor)"
                indicador = "✅ Normal"
            else:
                estado = "Normal"
                indicador = "✅ Normal"
        else:
            estado = "Atípico"
            indicador = "🟡 Revisar"

        sitio["INV_WATTS"] = round(load_power, 1) if load_power is not None else "N/A"
        sitio["MET_WATTS"] = round(met_power, 1) if met_power is not None else "N/A"
        sitio["ESTADO"] = estado
        sitio["INDICADOR"] = indicador

    except Exception:
        sitio["ESTADO"] = "Error API"
        sitio["INDICADOR"] = "🟣 Error"
        sitio["INV_WATTS"] = "N/A"
        sitio["MET_WATTS"] = "N/A"
        
    return sitio

# --- INTERFAZ ---
session = requests.Session()
session.headers.update({
    "X-STELLAR-OP": "hybrico", "X-STELLAR-ORG": "hybrico",
    "Origin": "https://stellar.newsunroad.com", "Referer": "https://stellar.newsunroad.com/",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

LOGIN_URL = "https://api.stellar.newsunroad.com/api/v0/login"

PAYLOAD = {
    "username": st.secrets["API_USERNAME"], 
    "password": st.secrets["API_PASSWORD"]
}

if "sitios_base" not in st.session_state:
    st.session_state.sitios_base = []

with st.sidebar:
    st.header("⚙️ Controles")
    if st.button("1. Cargar Topología de Red"):
        with st.spinner("Autenticando y mapeando sitios..."):
            session.post(LOGIN_URL, json=PAYLOAD, timeout=10)
            res_orgs = session.get("https://api.stellar.newsunroad.com/api/v0/organizations", timeout=15)
            org_list = res_orgs.json().get('data', []) if res_orgs.status_code == 200 else []
            
            sitios = []
            for org in org_list:
                nombre_org = org.get('name', 'Desconocida')
                if "tigo gt" in nombre_org.lower() or "honduras" in nombre_org.lower():
                    for view in org.get('powerSystemViews', []):
                        if isinstance(view, dict):
                            ps_id = view.get('viewOf')
                            if ps_id:
                                sitios.append({
                                    "REGION": nombre_org,
                                    "NOMBRE_SITIO": str(ps_id).replace('_', ' ').replace('-', ' ').upper(),
                                    "SITIO_ID": ps_id
                                })
            st.session_state.sitios_base = sitios
            st.success(f"Topología cargada: {len(sitios)} sitios.")

if not st.session_state.sitios_base:
    st.info("👈 Haz clic en 'Cargar Topología de Red' en la barra lateral para empezar.")
    st.stop()

regiones_disponibles = sorted(list(set([s["REGION"] for s in st.session_state.sitios_base])))
region_sel = st.selectbox("Selecciona la región a escanear:", regiones_disponibles)

if st.button(f"🚀 Iniciar Escaneo de Bypass y Medición en {region_sel}", type="primary"):
    session.post(LOGIN_URL, json=PAYLOAD, timeout=10)
    
    ahora_utc = datetime.now(timezone.utc)
    hace_una_hora = ahora_utc - timedelta(hours=1)
    end_sec = int(ahora_utc.timestamp())
    start_sec = int(hace_una_hora.timestamp())
    
    sitios_a_escanear = [s.copy() for s in st.session_state.sitios_base if s["REGION"] == region_sel]
    total_sitios = len(sitios_a_escanear)
    
    barra_progreso = st.progress(0)
    texto_progreso = st.empty()
    
    resultados = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        futuros = {executor.submit(escanear_sitio, sitio, session, start_sec, end_sec, ahora_utc): sitio for sitio in sitios_a_escanear}
        completados = 0
        
        for futuro in concurrent.futures.as_completed(futuros):
            resultados.append(futuro.result())
            completados += 1
            barra_progreso.progress(completados / total_sitios)
            texto_progreso.text(f"Escaneando... {completados} de {total_sitios} completados.")
            
    st.success("¡Escaneo finalizado!")
    
    df_resultados = pd.DataFrame(resultados)
    
    total = len(df_resultados)
    sin_gestion = len(df_resultados[df_resultados["ESTADO"] == "Sin Gestión"])
    en_bypass = len(df_resultados[df_resultados["ESTADO"] == "Bypass"])
    falla_medicion = len(df_resultados[df_resultados["ESTADO"] == "Falla Medición"])
    normales = total - sin_gestion - en_bypass - falla_medicion
    
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Escaneados", total)
    c2.metric("Normales ✅", normales)
    c3.metric("En Bypass ⚠️", en_bypass)
    c4.metric("Falla Medición 📐", falla_medicion)
    c5.metric("Sin Gestión / Data Gap 🔴", sin_gestion)
    
    st.markdown("### 📋 Resultados Detallados")
    st.dataframe(df_resultados[["REGION", "NOMBRE_SITIO", "INDICADOR", "INV_WATTS", "MET_WATTS", "SITIO_ID"]].sort_values(by="INDICADOR"), use_container_width=True, hide_index=True)