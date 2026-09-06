import difflib
import hashlib
import hmac
import os
import random
import re
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta
from email.message import EmailMessage

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

CATEGORIES = ["Boisson", "Pâtisserie", "Entretien", "Hygiène", "Autre"]
SIMILARITY_THRESHOLD = 0.82

def normalize_name(name):
    name = name.strip().lower().replace("’", "'")
    return re.sub(r"[\s'\-]+", "", name)

def find_duplicate(name):
    """Retourne ('exact', nom) si un article quasi-identique existe (bloquant),
    ('similar', nom) si un article ressemblant existe (à confirmer), sinon ('none', None)."""
    items_df = get_all_items()
    target_norm = normalize_name(name)
    best_name, best_ratio = None, 0.0
    for existing in items_df["name"]:
        existing_norm = normalize_name(existing)
        if existing_norm == target_norm:
            return "exact", existing
        ratio = difflib.SequenceMatcher(None, target_norm, existing_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_name = ratio, existing
    if best_ratio >= SIMILARITY_THRESHOLD:
        return "similar", best_name
    return "none", None

def init_db():
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    # Table des articles
    c.execute('''CREATE TABLE IF NOT EXISTS items
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL UNIQUE,
                  category TEXT NOT NULL DEFAULT 'Autre',
                  reorder_threshold INTEGER NOT NULL DEFAULT 2,
                  warning_threshold INTEGER NOT NULL DEFAULT 5)''')
    existing_cols = [row[1] for row in c.execute("PRAGMA table_info(items)").fetchall()]
    if "category" not in existing_cols:
        c.execute("ALTER TABLE items ADD COLUMN category TEXT NOT NULL DEFAULT 'Autre'")
    if "reorder_threshold" not in existing_cols:
        c.execute("ALTER TABLE items ADD COLUMN reorder_threshold INTEGER NOT NULL DEFAULT 2")
    if "warning_threshold" not in existing_cols:
        c.execute("ALTER TABLE items ADD COLUMN warning_threshold INTEGER NOT NULL DEFAULT 5")
    # Table des stocks par lieu
    c.execute('''CREATE TABLE IF NOT EXISTS stock
                 (item_id INTEGER,
                  location TEXT CHECK(location IN ('Cave', 'Cuisine')),
                  quantity INTEGER DEFAULT 0,
                  FOREIGN KEY(item_id) REFERENCES items(id),
                  PRIMARY KEY (item_id, location))''')
    # Table des comptes utilisateurs
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT NOT NULL UNIQUE,
                  salt TEXT NOT NULL,
                  password_hash TEXT NOT NULL,
                  reset_code_hash TEXT,
                  reset_code_expiry TEXT,
                  is_admin INTEGER NOT NULL DEFAULT 0)''')
    user_cols = [row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()]
    if "reset_code_hash" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN reset_code_hash TEXT")
    if "reset_code_expiry" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN reset_code_expiry TEXT")
    if "is_admin" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        # Le tout premier compte devient admin par défaut (celui qui a créé l'appli).
        c.execute(
            "UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)"
        )
    conn.commit()
    conn.close()

def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 200_000).hex()
    return salt, digest

def create_user(username, password):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    try:
        if c.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone():
            return False
        salt, digest = hash_password(password)
        c.execute("INSERT INTO users (username, salt, password_hash) VALUES (?, ?, ?)", (username, salt, digest))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def verify_user(username, password):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    row = c.execute(
        "SELECT username, salt, password_hash, is_admin FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    real_username, salt, stored_hash, is_admin = row
    _, digest = hash_password(password, salt)
    return (real_username, bool(is_admin)) if hmac.compare_digest(digest, stored_hash) else None

def hash_reset_code(code):
    return hashlib.sha256(code.encode('utf-8')).hexdigest()

def send_reset_email(to_address, code):
    smtp_conf = st.secrets["smtp"]
    msg = EmailMessage()
    msg["Subject"] = "App-Stock — Code de réinitialisation"
    msg["From"] = smtp_conf["email"]
    msg["To"] = to_address
    msg.set_content(
        f"Voici ton code de réinitialisation de mot de passe : {code}\n"
        "Ce code est valable 15 minutes."
    )
    with smtplib.SMTP(smtp_conf["server"], smtp_conf["port"]) as server:
        server.starttls()
        server.login(smtp_conf["email"], smtp_conf["password"])
        server.send_message(msg)

def request_password_reset(username):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    row = c.execute("SELECT username FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    if not row:
        conn.close()
        return False, "Aucun compte associé à ce nom d'utilisateur."
    real_username = row[0]
    code = f"{random.randint(0, 999999):06d}"
    expiry = (datetime.now() + timedelta(minutes=15)).isoformat()
    c.execute(
        "UPDATE users SET reset_code_hash = ?, reset_code_expiry = ? WHERE username = ?",
        (hash_reset_code(code), expiry, real_username),
    )
    conn.commit()
    conn.close()
    try:
        send_reset_email(real_username, code)
    except Exception as e:
        return False, f"Échec de l'envoi de l'email : {e}"
    return True, "Un code a été envoyé par email."

def reset_password_with_code(username, code, new_password):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    row = c.execute(
        "SELECT username, reset_code_hash, reset_code_expiry FROM users WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()
    if not row or not row[1]:
        conn.close()
        return False
    real_username, stored_code_hash, expiry = row
    if datetime.now() > datetime.fromisoformat(expiry) or not hmac.compare_digest(hash_reset_code(code), stored_code_hash):
        conn.close()
        return False
    salt, digest = hash_password(new_password)
    c.execute(
        "UPDATE users SET salt = ?, password_hash = ?, reset_code_hash = NULL, reset_code_expiry = NULL WHERE username = ?",
        (salt, digest, real_username),
    )
    conn.commit()
    conn.close()
    return True

def generate_temp_password():
    return secrets.token_urlsafe(9)

def send_new_account_email(to_address, temp_password):
    smtp_conf = st.secrets["smtp"]
    msg = EmailMessage()
    msg["Subject"] = "App-Stock — Ton compte a été créé"
    msg["From"] = smtp_conf["email"]
    msg["To"] = to_address
    msg.set_content(
        f"Un compte App-Stock a été créé pour toi.\n\n"
        f"Identifiant : {to_address}\n"
        f"Mot de passe temporaire : {temp_password}\n\n"
        "Tu peux le changer à tout moment via « Mot de passe oublié »."
    )
    with smtplib.SMTP(smtp_conf["server"], smtp_conf["port"]) as server:
        server.starttls()
        server.login(smtp_conf["email"], smtp_conf["password"])
        server.send_message(msg)

def create_user_by_admin(email):
    temp_password = generate_temp_password()
    if not create_user(email, temp_password):
        return False, "Ce nom d'utilisateur existe déjà."
    try:
        send_new_account_email(email, temp_password)
    except Exception as e:
        return False, f"Compte créé mais échec de l'envoi de l'email : {e}"
    return True, f"Compte créé pour {email}, ses accès lui ont été envoyés par email."

def get_all_stock():
    conn = sqlite3.connect('inventory.db')
    query = '''
    SELECT i.name, i.category,
           SUM(CASE WHEN s.location = 'Cave' THEN s.quantity ELSE 0 END) as Cave,
           SUM(CASE WHEN s.location = 'Cuisine' THEN s.quantity ELSE 0 END) as Cuisine,
           SUM(s.quantity) as Total
    FROM items i
    LEFT JOIN stock s ON i.id = s.item_id
    GROUP BY i.name, i.category
    '''
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def get_items_needing_reorder():
    conn = sqlite3.connect('inventory.db')
    query = '''
    SELECT i.name, i.reorder_threshold, COALESCE(SUM(s.quantity), 0) as total_quantity
    FROM items i
    LEFT JOIN stock s ON i.id = s.item_id
    GROUP BY i.name, i.reorder_threshold
    HAVING total_quantity <= i.reorder_threshold
    ORDER BY i.name
    '''
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def add_item(name, locations, category, reorder_threshold=2, warning_threshold=5):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    try:
        if c.execute("SELECT 1 FROM items WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            return False
        c.execute(
            "INSERT INTO items (name, category, reorder_threshold, warning_threshold) VALUES (?, ?, ?, ?)",
            (name, category, reorder_threshold, warning_threshold),
        )
        item_id = c.lastrowid
        for location in locations:
            c.execute("INSERT INTO stock (item_id, location, quantity) VALUES (?, ?, 0)", (item_id, location))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_stock_for_location(location):
    conn = sqlite3.connect('inventory.db')
    query = '''
    SELECT i.name, i.category, i.reorder_threshold, i.warning_threshold, s.quantity
    FROM items i
    JOIN stock s ON i.id = s.item_id AND s.location = ?
    ORDER BY i.name
    '''
    df = pd.read_sql_query(query, conn, params=(location,))
    conn.close()
    return df

def update_stock(item_name, location, delta):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    c.execute('''
        UPDATE stock
        SET quantity = MAX(0, quantity + ?)
        WHERE item_id = (SELECT id FROM items WHERE name = ?)
        AND location = ?
    ''', (delta, item_name, location))
    conn.commit()
    conn.close()

def set_stock(item_name, location, quantity):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    item_id = c.execute("SELECT id FROM items WHERE name = ?", (item_name,)).fetchone()[0]
    c.execute('''
        INSERT INTO stock (item_id, location, quantity) VALUES (?, ?, ?)
        ON CONFLICT(item_id, location) DO UPDATE SET quantity = excluded.quantity
    ''', (item_id, location, max(0, int(quantity))))
    conn.commit()
    conn.close()

def get_all_items():
    conn = sqlite3.connect('inventory.db')
    df = pd.read_sql_query(
        "SELECT name, category, reorder_threshold, warning_threshold FROM items ORDER BY name", conn
    )
    conn.close()
    return df

def update_item(old_name, new_name, category, reorder_threshold, warning_threshold):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    try:
        conflict = c.execute(
            "SELECT 1 FROM items WHERE name = ? COLLATE NOCASE AND name != ?", (new_name, old_name)
        ).fetchone()
        if conflict:
            return False
        c.execute(
            "UPDATE items SET name = ?, category = ?, reorder_threshold = ?, warning_threshold = ? WHERE name = ?",
            (new_name, category, reorder_threshold, warning_threshold, old_name),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def delete_item(name):
    conn = sqlite3.connect('inventory.db')
    c = conn.cursor()
    row = c.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone()
    if row:
        item_id = row[0]
        c.execute("DELETE FROM stock WHERE item_id = ?", (item_id,))
        c.execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
    conn.close()

# Interface Streamlit
st.set_page_config(page_title="App-Stock", layout="wide")

st.markdown("""
<style>
/* Réduit l'espace vide au-dessus du titre */
[data-testid="stMainBlockContainer"] {
    padding-top: 0.5rem;
}

/* Titre principal plus petit */
[data-testid="stAppViewContainer"] h1 {
    font-size: 1.6rem !important;
}

/* Sous-titres (État Global des Stocks / Stock : Cave / Stock : Cuisine) plus petits */
[data-testid="stAppViewContainer"] h2 {
    font-size: 1.2rem !important;
}

/* Onglets de navigation sur toute la largeur, avec pastille colorée sur l'onglet actif */
[data-testid="stTabs"] [role="tablist"] {
    display: flex;
    width: 100%;
    gap: 0 !important;
}
[data-testid="stTab"] {
    flex: 1;
    display: flex;
    justify-content: center;
    align-items: center;
    border-radius: 0 !important;
    margin: 0 !important;
    padding: 0.55rem 0.5rem !important;
    background-color: #FFF1E6;
    border: 1px solid rgba(0,0,0,0.06);
    box-shadow: 0 2px 4px rgba(0,0,0,0.08);
    transition: transform 0.1s ease, box-shadow 0.1s ease;
}
[data-testid="stTab"]:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(0,0,0,0.12);
}
[data-testid="stTab"] [data-testid="stMarkdownContainer"] {
    text-align: center;
    width: 100%;
}
[data-testid="stTab"] [data-testid="stMarkdownContainer"] p {
    font-weight: 600;
}
[data-testid="stTab"][aria-selected="true"] {
    background-color: #FF6B6B;
    border-color: #FF6B6B;
    box-shadow: 0 3px 6px rgba(230, 57, 70, 0.35);
}
[data-testid="stTab"][aria-selected="true"] [data-testid="stMarkdownContainer"] p {
    color: white !important;
    font-weight: 700;
}

/* Bandeau coloré derrière le titre */
.title-banner {
    background: linear-gradient(135deg, #FF6B6B 0%, #F4A261 100%);
    border-radius: 18px;
    padding: 0.7rem 1.5rem;
    margin-bottom: 0.4rem;
}
.title-banner h1 {
    color: white !important;
    margin: 0 !important;
}
.title-banner p {
    color: rgba(255,255,255,0.9) !important;
    margin: 0.2rem 0 0 0 !important;
}

/* Remplace la double flèche d'ouverture de la sidebar par un + entouré d'un cercle */
[data-testid="stExpandSidebarButton"] {
    border-radius: 50% !important;
    border: 2px solid #FF6B6B !important;
    background-color: #FFF1E6 !important;
    width: 40px !important;
    height: 40px !important;
    min-width: 40px !important;
    min-height: 40px !important;
}
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] {
    font-size: 0;
}
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"]::after {
    content: "+";
    font-family: sans-serif;
    font-size: 2rem;
    font-weight: 700;
    line-height: 1;
    color: #FF6B6B;
}

/* Boutons ronds et punchy pour les + / - */
button[kind="secondary"] {
    border-radius: 999px !important;
    font-weight: 700 !important;
    padding: 0.05rem 0.35rem !important;
    font-size: 0.65rem !important;
    min-height: 0 !important;
}
/* Le bouton "Ajouter" de la sidebar garde sa taille normale */
[data-testid="stSidebar"] button[kind="secondary"] {
    padding: 0.5rem 1rem !important;
    font-size: 1rem !important;
    min-height: 2.5rem !important;
}

/* Cartes d'articles */
.item-card {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.6rem 1rem;
    border-radius: 14px;
    margin-bottom: 0.5rem;
    min-width: 0;
}
.item-name {
    display: block;
    font-weight: 600;
    font-size: 1.4rem;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.reorder-item-card .item-name {
    font-size: 1rem;
}
.qty-badge {
    display: inline-block;
    padding: 0.2rem 0.7rem;
    border-radius: 999px;
    font-weight: 700;
    color: white;
    font-size: 0.9rem;
}
.qty-empty { background-color: #E63946; }
.qty-low { background-color: #F4A261; }
.qty-ok { background-color: #2A9D8F; }

.card-empty { background-color: #FDEDEC; border-left: 5px solid #E63946; }
.card-low { background-color: #FEF3E2; border-left: 5px solid #F4A261; }
.card-ok { background-color: #EAFBF3; border-left: 5px solid #2A9D8F; }

/* Colle les boutons +/- à la carte d'article, sur la même ligne, sans vide entre eux */
[data-testid="stHorizontalBlock"]:has(.item-card) {
    align-items: center;
    gap: 0.4rem !important;
    flex-wrap: nowrap !important;
    overflow-x: auto !important;
}
[data-testid="stHorizontalBlock"]:has(.item-card) > [data-testid="stColumn"]:nth-of-type(1) {
    min-width: 0 !important;
    overflow: hidden !important;
}
[data-testid="stHorizontalBlock"]:has(.item-card) > [data-testid="stColumn"]:nth-of-type(2),
[data-testid="stHorizontalBlock"]:has(.item-card) > [data-testid="stColumn"]:nth-of-type(3),
[data-testid="stHorizontalBlock"]:has(.item-card) > [data-testid="stColumn"]:nth-of-type(4) {
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: fit-content !important;
}
/* Boutons +/- des articles Cave/Cuisine : ronds, pleins, contraste vert/rouge */
[data-testid="stHorizontalBlock"]:has(.item-card) button {
    width: 34px !important;
    height: 34px !important;
    min-height: 34px !important;
    border-radius: 50% !important;
    padding: 0 !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.15) !important;
}
[data-testid="stHorizontalBlock"]:has(.item-card) button p {
    font-size: 1.3rem !important;
    font-weight: 700 !important;
    line-height: 1 !important;
    margin: 0 !important;
}
[data-testid="stHorizontalBlock"]:has(.item-card) button[kind="primary"] {
    background-color: #2A9D8F !important;
    border: none !important;
}
[data-testid="stHorizontalBlock"]:has(.item-card) button[kind="primary"] p {
    color: white !important;
}
[data-testid="stHorizontalBlock"]:has(.item-card) button[kind="secondary"] {
    background-color: #FDEDEC !important;
    border: 2px solid #E63946 !important;
}
[data-testid="stHorizontalBlock"]:has(.item-card) button[kind="secondary"] p {
    color: #E63946 !important;
}

/* Bouton + coloré (primary), bouton - discret (secondary) */
button[kind="primary"] {
    border-radius: 999px !important;
    font-weight: 700 !important;
    background-color: #2A9D8F !important;
    border-color: #2A9D8F !important;
    padding: 0.05rem 0.35rem !important;
    font-size: 0.65rem !important;
    min-height: 0 !important;
}

/* Les boutons de la page de connexion/inscription gardent leur taille normale */
[data-testid="stVerticalBlock"]:has(.auth-page-marker) button {
    padding: 0.5rem 1rem !important;
    font-size: 1rem !important;
    min-height: 2.5rem !important;
    border-radius: 999px !important;
}

/* Le bouton "Éditer un article" garde sa taille normale */
/* (le marqueur est niché dans son propre stElementContainer : il faut cibler
   le conteneur qui LE CONTIENT, pas le marqueur lui-même, pour que le "+" sibling
   combinator atteigne le conteneur du bouton juste après) */
[data-testid="stElementContainer"]:has(.edit-btn-marker) + [data-testid="stElementContainer"] button {
    padding: 0.5rem 1rem !important;
    font-size: 1rem !important;
    min-height: 2.5rem !important;
    border-radius: 999px !important;
}

/* Le bouton "À racheter" garde sa taille normale, et colle au bord droit */
[data-testid="stElementContainer"]:has(.reorder-btn-marker) + [data-testid="stElementContainer"] {
    text-align: right !important;
    width: 100% !important;
}
[data-testid="stElementContainer"]:has(.reorder-btn-marker) + [data-testid="stElementContainer"] button {
    padding: 0.5rem 1rem !important;
    font-size: 1rem !important;
    min-height: 2.5rem !important;
    border-radius: 999px !important;
    width: fit-content !important;
}

/* Menu compte (⋮) : rond, même taille que le "+" d'ouverture de la sidebar, collé au bord droit */
/* (st.popover s'enveloppe dans un stLayoutWrapper, pas un stElementContainer comme st.button) */
[data-testid="stElementContainer"]:has(.account-menu-marker) + [data-testid="stLayoutWrapper"] {
    text-align: right !important;
    width: 100% !important;
    margin-bottom: 0.5rem !important;
}
[data-testid="stElementContainer"]:has(.account-menu-marker) + [data-testid="stLayoutWrapper"] button {
    width: 40px !important;
    height: 40px !important;
    min-height: 40px !important;
    padding: 0 !important;
    border-radius: 50% !important;
    border: 2px solid #FF6B6B !important;
    background-color: #FFF1E6 !important;
}
[data-testid="stElementContainer"]:has(.account-menu-marker) + [data-testid="stLayoutWrapper"] button p {
    font-size: 1.5rem !important;
    font-weight: 700 !important;
    line-height: 1 !important;
    color: #FF6B6B !important;
    margin: 0 !important;
}
/* Cache le chevron "expand_more" automatique du popover, ne garde que "⋮" */
[data-testid="stElementContainer"]:has(.account-menu-marker) + [data-testid="stLayoutWrapper"] [data-testid="stIconMaterial"] {
    display: none !important;
}

/* En écran étroit (mobile), Streamlit empile normalement les colonnes : on force
   les deux boutons à rester l'un à côté de l'autre, dimensionnés à leur contenu
   (jamais rétrécis/coupés) ; si vraiment trop étroit, la ligne défile au lieu de
   masquer un bouton. Le style desktop (colonnes 3:1, "À racheter" à droite) reste
   inchangé au-dessus de ce seuil. */
@media (max-width: 700px) {
    [data-testid="stHorizontalBlock"]:has(.edit-btn-marker):has(.reorder-btn-marker) {
        flex-wrap: nowrap !important;
        overflow-x: auto !important;
    }
    [data-testid="stHorizontalBlock"]:has(.edit-btn-marker):has(.reorder-btn-marker) > [data-testid="stColumn"] {
        flex: 0 0 auto !important;
        width: auto !important;
        min-width: fit-content !important;
    }
    [data-testid="stElementContainer"]:has(.reorder-btn-marker) + [data-testid="stElementContainer"] {
        width: auto !important;
        text-align: left !important;
    }
}

/* Les boutons dans la pop-up d'édition gardent leur taille normale */
[data-testid="stDialog"] button, [role="dialog"] button {
    padding: 0.5rem 1rem !important;
    font-size: 1rem !important;
    min-height: 2.5rem !important;
}
</style>
""", unsafe_allow_html=True)

init_db()

if "authenticated_user" not in st.session_state:
    st.session_state.authenticated_user = None
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "auth_mode" not in st.session_state:
    st.session_state.auth_mode = "login"

if not st.session_state.authenticated_user:
    _, auth_col, _ = st.columns([1, 1.3, 1])
    with auth_col:
        st.markdown(
            """<div class="auth-page-marker"></div>
            <div class="title-banner">
                <h1>📦 App-Stock</h1>
                <p>Toujours savoir ce qu'il reste, sans descendre à la cave 😉</p>
            </div>""",
            unsafe_allow_html=True,
        )
        if st.session_state.auth_mode == "login":
            st.subheader("🔑 Connexion")
            login_username = st.text_input("Nom d'utilisateur", key="login_username")
            login_password = st.text_input("Mot de passe", type="password", key="login_password")
            if st.button("Se connecter", type="primary", key="login_submit", use_container_width=True):
                verified = verify_user(login_username, login_password)
                if verified:
                    st.session_state.authenticated_user, st.session_state.is_admin = verified
                    st.rerun()
                else:
                    st.error("Nom d'utilisateur ou mot de passe incorrect.")
            if st.button("Mot de passe oublié ?", key="switch_to_forgot", use_container_width=True):
                st.session_state.auth_mode = "forgot_request"
                st.rerun()
        elif st.session_state.auth_mode == "forgot_request":
            st.subheader("🔓 Mot de passe oublié")
            forgot_username = st.text_input("Nom d'utilisateur", key="forgot_username")
            if st.button("Envoyer le code par email", type="primary", key="forgot_send", use_container_width=True):
                sent, message = request_password_reset(forgot_username)
                if sent:
                    st.session_state.reset_username = forgot_username
                    st.session_state.auth_mode = "forgot_reset"
                    st.rerun()
                else:
                    st.error(message)
            if st.button("Retour à la connexion", key="back_to_login_from_request", use_container_width=True):
                st.session_state.auth_mode = "login"
                st.rerun()
        else:
            st.subheader("🔓 Réinitialiser le mot de passe")
            st.caption(f"Un code a été envoyé à **{st.session_state.get('reset_username', '')}**.")
            reset_code = st.text_input("Code reçu par email", key="reset_code")
            reset_new_password = st.text_input("Nouveau mot de passe", type="password", key="reset_new_password")
            reset_new_password_confirm = st.text_input(
                "Confirmer le nouveau mot de passe", type="password", key="reset_new_password_confirm"
            )
            if st.button("Réinitialiser le mot de passe", type="primary", key="reset_submit", use_container_width=True):
                if not reset_code or not reset_new_password:
                    st.error("Merci de remplir tous les champs.")
                elif reset_new_password != reset_new_password_confirm:
                    st.error("Les mots de passe ne correspondent pas.")
                elif reset_password_with_code(st.session_state.get("reset_username", ""), reset_code, reset_new_password):
                    st.success("Mot de passe réinitialisé ! Tu peux te connecter.")
                    st.session_state.auth_mode = "login"
                else:
                    st.error("Code invalide ou expiré.")
            if st.button("Retour à la connexion", key="back_to_login_from_reset", use_container_width=True):
                st.session_state.auth_mode = "login"
                st.rerun()
    st.stop()

st.markdown('<div class="account-menu-marker"></div>', unsafe_allow_html=True)
with st.popover("⋮"):
    st.write(f"Connecté : `{st.session_state.authenticated_user}`")
    if st.button("Se déconnecter", key="logout_button", use_container_width=True):
        st.session_state.authenticated_user = None
        st.session_state.is_admin = False
        st.rerun()
    if st.session_state.is_admin:
        st.divider()
        st.write("**Créer un utilisateur**")
        new_user_email = st.text_input("Email", key="admin_new_user_email")
        if st.button("Créer et envoyer les accès", key="admin_create_user", use_container_width=True):
            if not new_user_email:
                st.error("Merci de renseigner un email.")
            else:
                created, message = create_user_by_admin(new_user_email)
                if created:
                    st.success(message)
                else:
                    st.error(message)

st.markdown(
    """<div class="title-banner">
        <h1>📦 App-Stock</h1>
        <p>Toujours savoir ce qu'il reste, sans descendre à la cave 😉</p>
    </div>""",
    unsafe_allow_html=True,
)

def render_inline_quantity_editor(item_name, location, current_qty, cell_token):
    if st.session_state.get("qty_editor_last_cell") != cell_token:
        st.session_state["qty_editor_value"] = current_qty
        st.session_state["qty_editor_last_cell"] = cell_token
    with st.container(border=True):
        header_col, close_col = st.columns([5, 1])
        header_col.write(f"**{item_name}** — {location}")
        if close_col.button("✖", key="qty_editor_close"):
            st.session_state["vue_globale_table"] = {"selection": {"cells": []}}
            st.rerun()
        new_qty = st.number_input("Quantité", min_value=0, step=1, key="qty_editor_value")
        if st.button("💾 Enregistrer", type="primary", key="qty_editor_save"):
            set_stock(item_name, location, new_qty)
            st.rerun()

@st.dialog("Éditer un article")
def edit_article_dialog():
    dialog_search = st.text_input("🔍 Rechercher un article", key="edit_dialog_search", placeholder="Nom de l'article...")
    items_df = get_all_items()
    if dialog_search:
        items_df = items_df[items_df["name"].str.contains(dialog_search, case=False, na=False)]
    items_df = items_df.reset_index(drop=True)

    options = items_df["name"].tolist()
    if not options:
        st.info("Aucun article trouvé.")
        return

    with st.container(height=250):
        for _, row in items_df.iterrows():
            if st.button(
                f"{row['name']} — {row['category']}",
                key=f"edit_pick_{row['name']}",
                use_container_width=True,
            ):
                st.session_state["edit_dialog_select"] = row["name"]

    if st.session_state.get("edit_dialog_select") not in options:
        st.session_state["edit_dialog_select"] = options[0]

    selected_name = st.selectbox("Article à modifier", options, key="edit_dialog_select")

    current_category = items_df.loc[items_df["name"] == selected_name, "category"].iloc[0]
    current_threshold = int(items_df.loc[items_df["name"] == selected_name, "reorder_threshold"].iloc[0])
    current_warning = int(items_df.loc[items_df["name"] == selected_name, "warning_threshold"].iloc[0])

    if st.session_state.get("edit_dialog_last_selected") != selected_name:
        st.session_state["edit_dialog_name"] = selected_name
        st.session_state["edit_dialog_category"] = current_category if current_category in CATEGORIES else CATEGORIES[0]
        st.session_state["edit_dialog_threshold"] = current_threshold
        st.session_state["edit_dialog_warning"] = current_warning
        st.session_state["edit_dialog_last_selected"] = selected_name

    new_name = st.text_input("Nom", key="edit_dialog_name")
    new_category = st.selectbox("Catégorie", CATEGORIES, key="edit_dialog_category")
    new_threshold = st.number_input(
        "Seuil de réachat (racheter si le stock descend à ce niveau ou en dessous)",
        min_value=0, step=1, key="edit_dialog_threshold",
    )
    new_warning = st.number_input(
        "Seuil d'attention (orange si le stock descend à ce niveau ou en dessous, sans être au seuil de réachat)",
        min_value=0, step=1, key="edit_dialog_warning",
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("💾 Enregistrer", type="primary", key="edit_dialog_save"):
            if update_item(selected_name, new_name, new_category, new_threshold, new_warning):
                st.rerun()
            else:
                st.error("Un article avec ce nom existe déjà.")
    with col2:
        if st.button("🗑️ Supprimer l'article", key="edit_dialog_delete"):
            delete_item(selected_name)
            st.rerun()

@st.dialog("🛒 Articles à racheter")
def reorder_list_dialog():
    df = get_items_needing_reorder()
    if df.empty:
        st.success("Aucun article à racheter pour le moment 🎉")
        return

    st.caption(f"{len(df)} article(s) sous le seuil de réachat")
    for _, row in df.iterrows():
        st.markdown(
            f"""<div class="item-card card-empty reorder-item-card">
                <span class="item-name">{row['name']}</span>
                <span class="qty-badge qty-empty">{row['total_quantity']}</span>
            </div>""",
            unsafe_allow_html=True,
        )

header_action_col1, header_action_col2 = st.columns([3, 1])
with header_action_col1:
    st.markdown('<div class="edit-btn-marker"></div>', unsafe_allow_html=True)
    if st.button("✏️ Éditer un article", key="open_edit_dialog"):
        edit_article_dialog()
with header_action_col2:
    st.markdown('<div class="reorder-btn-marker"></div>', unsafe_allow_html=True)
    if st.button("🛒 À racheter", key="open_reorder_dialog"):
        reorder_list_dialog()

# Sidebar pour ajouter un article
st.sidebar.header("Nouvel Article")
new_item = st.sidebar.text_input("Nom de l'article")
new_item_location = st.sidebar.radio(
    "Où ranger cet article ?",
    ["Cave", "Cuisine", "Les deux"],
    index=2,
    horizontal=True,
)
new_item_category = st.sidebar.selectbox("Catégorie", CATEGORIES)
new_item_threshold = st.sidebar.number_input(
    "Seuil de réachat (racheter si le stock descend à ce niveau ou en dessous)",
    min_value=0, value=2, step=1,
)
new_item_warning = st.sidebar.number_input(
    "Seuil d'attention (orange si le stock descend à ce niveau ou en dessous, sans être au seuil de réachat)",
    min_value=0, value=5, step=1,
)

if "pending_new_item" not in st.session_state:
    st.session_state.pending_new_item = None

if st.sidebar.button("Ajouter"):
    if new_item:
        locations = ["Cave", "Cuisine"] if new_item_location == "Les deux" else [new_item_location]
        dup_type, dup_name = find_duplicate(new_item)
        if dup_type == "exact":
            st.sidebar.error("L'article existe déjà.")
        elif dup_type == "similar":
            st.session_state.pending_new_item = {
                "name": new_item,
                "locations": locations,
                "category": new_item_category,
                "threshold": new_item_threshold,
                "warning": new_item_warning,
                "similar_to": dup_name,
            }
        else:
            if add_item(new_item, locations, new_item_category, new_item_threshold, new_item_warning):
                st.sidebar.success(f"{new_item} ajouté !")
            else:
                st.sidebar.error("L'article existe déjà.")

if st.session_state.pending_new_item:
    pending = st.session_state.pending_new_item
    st.sidebar.warning(f"Article similaire trouvé : **{pending['similar_to']}**. Ajouter « {pending['name']} » quand même ?")
    col1, col2 = st.sidebar.columns(2)
    with col1:
        if st.button("Ajouter quand même", key="confirm_similar_add"):
            add_item(pending["name"], pending["locations"], pending["category"], pending["threshold"], pending["warning"])
            st.session_state.pending_new_item = None
            st.rerun()
    with col2:
        if st.button("Annuler", key="cancel_similar_add"):
            st.session_state.pending_new_item = None
            st.rerun()

selected_category = st.pills("Catégorie", CATEGORIES)

search_query = st.text_input("🔍 Rechercher un article", placeholder="Nom de l'article...")
components.html(
    """
    <script>
    function attachLiveSearch() {
        const doc = window.parent.document;
        const input = doc.querySelector('input[placeholder="Nom de l\\'article..."]');
        if (!input || input.dataset.liveSearchAttached) return;
        input.dataset.liveSearchAttached = "true";
        let timer = null;
        input.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
            }, 400);
        });
    }
    attachLiveSearch();
    new MutationObserver(attachLiveSearch).observe(window.parent.document.body, {childList: true, subtree: true});
    </script>
    """,
    height=0,
)

# Navigation principale
tab1, tab2, tab3 = st.tabs(["🏠 Vue Globale", "📦 Cave", "👨‍🍳 Cuisine"])

df_stock = get_all_stock()
if selected_category:
    df_stock = df_stock[df_stock["category"] == selected_category]
if search_query:
    df_stock = df_stock[df_stock["name"].str.contains(search_query, case=False, na=False)]

def render_location_view(location):
    st.header(f"Stock : {location}")
    df = get_stock_for_location(location)
    if selected_category:
        df = df[df["category"] == selected_category]
    if search_query:
        df = df[df["name"].str.contains(search_query, case=False, na=False)]
    for index, row in df.iterrows():
        qty = row["quantity"]
        if qty <= row["reorder_threshold"]:
            badge_class, card_class = "qty-empty", "card-empty"
        elif qty <= row["warning_threshold"]:
            badge_class, card_class = "qty-low", "card-low"
        else:
            badge_class, card_class = "qty-ok", "card-ok"

        col_name, col_plus, col_minus, col_qty = st.columns([4, 1, 1, 1], gap="small")
        with col_name:
            st.markdown(
                f"""<div class="item-card {card_class}">
                    <span class="item-name">{row['name']}</span>
                </div>""",
                unsafe_allow_html=True,
            )
        with col_plus:
            if st.button("+", key=f"plus_{location}_{row['name']}", type="primary"):
                update_stock(row['name'], location, 1)
                st.rerun()
        with col_minus:
            if st.button("−", key=f"minus_{location}_{row['name']}"):
                update_stock(row['name'], location, -1)
                st.rerun()
        with col_qty:
            st.markdown(f'<span class="qty-badge {badge_class}">{qty}</span>', unsafe_allow_html=True)

with tab1:
    st.header("État Global des Stocks")
    st.caption("Cliquer sur le nom d'un article pour l'éditer, ou sur une quantité (Cave ou Cuisine) pour la modifier.")
    base_stock = df_stock.drop(columns=["category"]).rename(columns={"name": "Nom"}).reset_index(drop=True)

    # Lit la sélection de la précédente interaction (déjà synchronisée par Streamlit
    # avant l'exécution du script) pour afficher l'éditeur AVANT de redessiner le
    # tableau, donc visuellement au-dessus de celui-ci.
    prev_cells = st.session_state.get("vue_globale_table", {}).get("selection", {}).get("cells", [])
    if prev_cells:
        row_idx, col_name = prev_cells[0]
        if row_idx < len(base_stock):
            item_name = base_stock.iloc[row_idx]["Nom"]
            if col_name in ("Cave", "Cuisine"):
                current_qty = int(base_stock.iloc[row_idx][col_name])
                render_inline_quantity_editor(item_name, col_name, current_qty, f"{item_name}_{col_name}")
            elif col_name == "Nom":
                if st.session_state.get("vue_globale_edit_last_opened") != item_name:
                    st.session_state["vue_globale_edit_last_opened"] = item_name
                    st.session_state["edit_dialog_select"] = item_name
                    edit_article_dialog()

    st.dataframe(
        base_stock,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-cell",
        key="vue_globale_table",
    )

with tab2:
    render_location_view("Cave")

with tab3:
    render_location_view("Cuisine")
