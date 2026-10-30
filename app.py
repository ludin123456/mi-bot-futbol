import math
import os
import unicodedata
from datetime import date

import pandas as pd
import requests
import streamlit as st

API = 'https://api.football-data.org/v4'
COMPETITIONS = {
    'España — La Liga': 'PD',
    'Inglaterra — Premier League': 'PL',
    'Italia — Serie A': 'SA',
    'Alemania — Bundesliga': 'BL1',
    'Francia — Ligue 1': 'FL1',
    'Portugal — Primeira Liga': 'PPL',
    'Países Bajos — Eredivisie': 'DED',
}

st.set_page_config(page_title='Bot Analista de Fútbol', page_icon='⚽', layout='wide')
st.title('⚽ Bot Analista de Fútbol')
st.caption('Probabilidades estimadas con datos de partidos reales. No hay resultados garantizados.')


def clean(s):
    s = unicodedata.normalize('NFD', s.lower())
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn').strip()


@st.cache_data(ttl=1800, show_spinner=False)
def get_matches(competition, token):
    try:
        r = requests.get(f'{API}/competitions/{competition}/matches',
                         headers={'X-Auth-Token': token}, timeout=20)
        r.raise_for_status()
        payload = r.json()
        return payload.get('matches', [])
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response else '?'
        raise RuntimeError(f'Error de la fuente de datos (HTTP {status}). Revisa tu clave API y la cobertura de tu plan.') from exc
    except (requests.exceptions.RequestException, ValueError) as exc:
        raise RuntimeError('No se pudo conectar con el proveedor de datos. Comprueba tu conexión.') from exc


def finished_data(matches):
    good = []
    for m in matches:
        ft = m.get('score', {}).get('fullTime') or {}
        h, a = ft.get('home'), ft.get('away')
        if m.get('status') == 'FINISHED' and isinstance(h, int) and isinstance(a, int):
            good.append({'date': m.get('utcDate', ''),
                         'hid': m['homeTeam']['id'], 'aid': m['awayTeam']['id'],
                         'home': m['homeTeam']['name'], 'away': m['awayTeam']['name'],
                         'hg': h, 'ag': a})
    return sorted(good, key=lambda x: x['date'])


def average(values, fallback):
    return sum(values) / len(values) if values else fallback


def team_record(games, tid, number=5):
    subset = [g for g in games if g['hid'] == tid or g['aid'] == tid][-number:]
    data = []
    for g in subset:
        home = g['hid'] == tid
        gf, ga = (g['hg'], g['ag']) if home else (g['ag'], g['hg'])
        data.append({'Fecha': g['date'][:10], 'Rival': g['away'] if home else g['home'],
                     'Condición': 'Local' if home else 'Visitante',
                     'Marcador': f'{gf}-{ga}',
                     'Resultado': 'G' if gf > ga else ('E' if gf == ga else 'P'),
                     'gf': gf, 'ga': ga})
    return data


def expected_goals(games, hid, aid):
    league_home = average([g['hg'] for g in games], 1.4)
    league_away = average([g['ag'] for g in games], 1.1)
    hlast = team_record(games, hid)
    alast = team_record(games, aid)
    if len(hlast) < 3 or len(alast) < 3:
        raise ValueError('Se necesitan al menos 3 partidos terminados por equipo para estimar.')
    h_home = [g for g in games if g['hid'] == hid][-10:]
    a_away = [g for g in games if g['aid'] == aid][-10:]
    # Regularización: mezcla rendimiento reciente, rendimiento local/visitante y promedio de liga.
    h_attack = (0.45 * average([x['gf'] for x in hlast], league_home)
                + 0.35 * average([x['hg'] for x in h_home], league_home) + 0.20 * league_home)
    h_def = (0.45 * average([x['ga'] for x in hlast], league_away)
             + 0.35 * average([x['ag'] for x in h_home], league_away) + 0.20 * league_away)
    a_attack = (0.45 * average([x['gf'] for x in alast], league_away)
                + 0.35 * average([x['ag'] for x in a_away], league_away) + 0.20 * league_away)
    a_def = (0.45 * average([x['ga'] for x in alast], league_home)
             + 0.35 * average([x['hg'] for x in a_away], league_home) + 0.20 * league_home)
    home_xg = max(0.15, min(4.0, h_attack * a_def / max(league_home, 0.5)))
    away_xg = max(0.15, min(4.0, a_attack * h_def / max(league_away, 0.5)))
    return home_xg, away_xg, hlast, alast


def poisson(k, rate):
    return math.exp(-rate) * rate ** k / math.factorial(k)


def probabilities(lh, la):
    # Cortamos en 12 goles; normalizamos para que los tres resultados sumen 100%.
    local = draw = visitor = over25 = btts = 0.0
    scores = []
    for h in range(13):
        for a in range(13):
            p = poisson(h, lh) * poisson(a, la)
            local += p if h > a else 0
            draw += p if h == a else 0
            visitor += p if h < a else 0
            over25 += p if h + a >= 3 else 0
            btts += p if h > 0 and a > 0 else 0
            scores.append((p, h, a))
    total = local + draw + visitor
    return ([x / total for x in (local, draw, visitor)], over25 / total,
            btts / total, sorted(scores, reverse=True)[:3])


def count_form(recs):
    return ' '.join(r['Resultado'] for r in recs) or 'Sin datos'


st.markdown('**Paso 1.** Consigue gratis una clave de resultados en [football-data.org](https://www.football-data.org/client/register). Algunas ligas o datos pueden requerir un plan de pago.')
token = st.text_input('Clave API de football-data.org', value=os.getenv('FOOTBALL_DATA_TOKEN', ''), type='password')
competition_name = st.selectbox('Liga', list(COMPETITIONS.keys()))
if not token:
    st.info('Introduce tu clave para cargar los partidos. La clave no se guarda en ningún archivo de este bot.')
    st.stop()

try:
    matches = get_matches(COMPETITIONS[competition_name], token.strip())
except RuntimeError as exc:
    st.error(str(exc))
    st.stop()

complete = finished_data(matches)
if not complete:
    st.warning('No hay partidos finalizados accesibles para esta liga o temporada.')
    st.stop()

team_names = {}
for m in matches:
    for side in ('homeTeam', 'awayTeam'):
        t = m.get(side) or {}
        if t.get('id') and t.get('name'):
            team_names[t['id']] = t['name']
ids = sorted(team_names, key=lambda x: clean(team_names[x]))
st.markdown('**Paso 2.** Selecciona los equipos. El primero juega en casa.')
c1, c2 = st.columns(2)
with c1:
    hid = st.selectbox('Equipo local 🏠', ids, format_func=lambda x: team_names[x], index=0)
with c2:
    aid = st.selectbox('Equipo visitante ✈️', ids, format_func=lambda x: team_names[x], index=min(1, len(ids)-1))

if hid == aid:
    st.warning('Selecciona dos equipos distintos.')
    st.stop()

upcoming = [m for m in matches if m.get('status') in ('SCHEDULED', 'TIMED')
            and m.get('homeTeam', {}).get('id') == hid and m.get('awayTeam', {}).get('id') == aid]
if upcoming:
    st.info('Próximo partido registrado: ' + upcoming[0]['utcDate'][:16].replace('T', ' ') + ' UTC')
else:
    st.caption('No se ha confirmado aquí un próximo enfrentamiento. El análisis es hipotético con el local y visitante seleccionados.')

st.markdown('**Paso 3 (opcional).** Introduce cuotas decimales del mercado 1-X-2 para buscar posibles diferencias.')
a, b, c = st.columns(3)
with a: odd_home = st.number_input('Cuota local (1)', 1.01, 100.0, 2.00, 0.05)
with b: odd_draw = st.number_input('Cuota empate (X)', 1.01, 100.0, 3.20, 0.05)
with c: odd_away = st.number_input('Cuota visitante (2)', 1.01, 100.0, 3.50, 0.05)
use_odds = st.checkbox('Comparar con estas cuotas (desmarcado por defecto)', value=False)

if st.button('🔎 ANALIZAR PARTIDO', type='primary', use_container_width=True):
    # Se excluyen juegos futuros por definición; datos solo de temporada actual de la fuente.
    try:
        lh, la, hlast, alast = expected_goals(complete, hid, aid)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    probs, over, btts, likely = probabilities(lh, la)
    labels = [team_names[hid], 'Empate', team_names[aid]]
    st.subheader(f'{team_names[hid]} vs. {team_names[aid]}')
    x, y, z = st.columns(3)
    x.metric('Victoria local', f'{probs[0]*100:.1f}%')
    y.metric('Empate', f'{probs[1]*100:.1f}%')
    z.metric('Victoria visitante', f'{probs[2]*100:.1f}%')
    st.bar_chart(pd.DataFrame({'Probabilidad': [round(p * 100, 1) for p in probs]}, index=labels))
    outcome = max(range(3), key=lambda i: probs[i])
    st.write(f'**Resultado más probable según el modelo:** {labels[outcome]}.')
    st.write(f'**Goles esperados (modelo Poisson):** {lh:.2f} local / {la:.2f} visitante.')
    st.write(f'**Más de 2.5 goles:** {over*100:.1f}% · **Ambos marcan:** {btts*100:.1f}%')
    st.write('**Marcadores exactos más probables:** ' + ', '.join(f'{h}-{a} ({p*100:.1f}%)' for p, h, a in likely))
    st.write(f'**Forma reciente, últimos {len(hlast)} partidos:** {team_names[hid]}: {count_form(hlast)}; {team_names[aid]}: {count_form(alast)}. (G=ganó, E=empató, P=perdió)')
    st.subheader('Últimos partidos de cada equipo')
    left, right = st.columns(2)
    with left:
        st.markdown(f'**{team_names[hid]}**')
        st.dataframe(pd.DataFrame(hlast).drop(columns=['gf','ga']), hide_index=True, use_container_width=True)
    with right:
        st.markdown(f'**{team_names[aid]}**')
        st.dataframe(pd.DataFrame(alast).drop(columns=['gf','ga']), hide_index=True, use_container_width=True)
    h2h = [g for g in complete if {g['hid'], g['aid']} == {hid, aid}][-5:]
    st.subheader('Enfrentamientos directos disponibles')
    if h2h:
        st.dataframe(pd.DataFrame([{'Fecha': g['date'][:10], 'Partido': f"{g['home']} {g['hg']}-{g['ag']} {g['away']}"} for g in h2h]), hide_index=True)
    else:
        st.caption('No hay enfrentamientos directos en los datos consultados de esta temporada.')
    if use_odds:
        st.subheader('Comparación con las cuotas que introdujiste')
        rows = []
        implied = [1/o for o in (odd_home, odd_draw, odd_away)]
        for label, p, odd, imp in zip(labels, probs, (odd_home, odd_draw, odd_away), implied):
            rows.append({'Resultado': label, 'Modelo %': round(100*p, 1),
                         'Implícita cuota %': round(100*imp, 1),
                         'EV teórico por $1': round(p*odd-1, 3),
                         'Cuota justa modelo': round(1/p, 2)})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption('La probabilidad implícita contiene margen del operador. EV positivo indica valor solo si el modelo está bien calibrado; no garantiza ganancias.')
    st.warning('**Limitaciones importantes:** estimación estadística, NO pronóstico seguro. Usa resultados de la temporada disponible y promedios recientes. No incorpora bajas, alineaciones, suspensiones, xG oficiales, cuotas en vivo ni noticias; no valida por sí mismo si el partido se jugará. Sin backtesting/calibración no hay nivel de confianza estadística verificado.')
    st.caption(f'Fuente: football-data.org | Partidos terminados disponibles: {len(complete)} | Consulta: {date.today().isoformat()}')
