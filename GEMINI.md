# App Stock - Suivi du Projet

Ce fichier permet de suivre l'avancement du projet, l'architecture et les tâches restantes pour l'application **App Stock**.

## 📌 Présentation du Projet
**App Stock** est une application simple et responsive de gestion d'inventaire pour la maison (boîtes de conserve, papier toilette, épicerie, etc.) répartie sur deux lieux : la **Cave** et la **Cuisine**.
L'application est optimisée pour une utilisation fluide sur mobile (iPhone & Android) et conçue pour être déployée sur un **VPS Ionos** via Docker.

### Technologies utilisées
- **Python 3.x**
- **Streamlit** (Interface utilisateur web réactive et nativement adaptée aux smartphones)
- **Pandas** (Manipulation de données de stock)
- **SQLite3** (Base de données locale persistante `inventory.db`)
- **Docker & Docker Compose** (Pour un déploiement robuste et identique en local et sur VPS)

---

## 🛠️ Architecture de la Base de Données
La base de données contient deux tables principales :
1. **`items`** : Liste des articles uniques (clé primaire `id`, `name` unique).
2. **`stock`** : Quantités d'articles par emplacement (clé primaire composite `item_id`, `location` limitée à `'Cave'` ou `'Cuisine'`).

---

## 📈 État d'Avancement et Road-map

### ✅ Réalisé
- [x] Cadrage du projet (utilisation de Streamlit pour la simplicité et la compatibilité mobile)
- [x] Initialisation du projet et de la base de données SQLite (`inventory.db`)
- [x] Configuration Docker (`Dockerfile` et `docker-compose.yml`)
- [x] Écriture du script principal `app.py` pour l'interface Streamlit (onglets Vue Globale, Cave et Cuisine)
- [x] Gestion des articles (ajout avec choix d'emplacement Cave/Cuisine/Les deux et de catégorie)
- [x] Ajustements rapides des stocks avec boutons de type `➕` et `➖` adaptés au mobile
- [x] Design complet : bandeau dégradé, onglets carrés pleine largeur, badges colorés selon quantité, thème via `.streamlit/config.toml`
- [x] Édition directe des quantités dans "Vue Globale" (tableau éditable)
- [x] Recherche dynamique (filtre lettre par lettre, sans Entrée) + bande de catégories cliquable (Boisson, Pâtisserie, Entretien, Hygiène, Autre)
- [x] Pop-up "Éditer un article" : liste cliquable, renommage, changement de catégorie, suppression
- [x] Anti-doublons à l'ajout : blocage si nom identique (insensible casse), confirmation si nom très ressemblant (fautes de frappe)

### ⏳ En Cours / Prochaines Étapes
- [ ] Vérifier le bon fonctionnement local via Docker (Docker pas encore installé sur ce poste)
- [ ] Déploiement sur le VPS Ionos (installation de Docker, transfert des fichiers, lancement) — l'accès mobile réel ne sera possible qu'à ce moment
- [ ] Sécurisation d'accès (optionnel : ajout d'un mot de passe simple si l'application est exposée en ligne)

---

## 🚀 Comment lancer le projet en local

### Dev local (sans Docker)
Docker n'est pas installé sur ce poste et l'antivirus **Norton** bloque le lancement d'un serveur exposé sur `0.0.0.0` (fenêtre d'alerte pare-feu). Le fichier `.streamlit/config.toml` restreint donc le serveur à `127.0.0.1` et coupe la télémétrie Streamlit :
```bash
# Installation des dépendances
pip install -r requirements.txt

# Lancement de l'application Streamlit
streamlit run app.py
```
L'application sera disponible sur `http://127.0.0.1:8501` (ce PC uniquement, pas d'accès mobile en dev).

### Avec Docker (réservé au VPS)
```bash
# Lancer l'application avec Docker Compose
docker-compose up --build -d
```
Sur le VPS, l'application sera disponible sur `http://<IP_DU_VPS>:8501`.

---

## 🌐 Déploiement sur VPS Ionos

Voici la procédure recommandée pour déployer l'application sur votre VPS Ionos :

1. **Se connecter au VPS en SSH :**
   ```bash
   ssh root@<IP_DE_VOTRE_VPS>
   ```

2. **Installer Docker et Docker Compose sur le VPS (si ce n'est pas déjà fait) :**
   ```bash
   # Mettre à jour les paquets
   apt update && apt upgrade -y

   # Installer Docker
   curl -fsSL https://get.docker.com -o get-docker.sh
   sh get-docker.sh

   # Installer Docker Compose
   apt install docker-compose -y
   ```

3. **Transférer les fichiers du projet sur le VPS :**
   Vous pouvez cloner le projet via Git, ou transférer les fichiers nécessaires (`app.py`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`) à l'aide de `scp` ou de FileZilla :
   ```bash
   # Exemple de transfert SSH
   scp -r . root@<IP_DE_VOTRE_VPS>:/root/app-stock
   ```

4. **Lancer l'application sur le VPS :**
   ```bash
   cd /root/app-stock
   docker-compose up --build -d
   ```

5. **Accéder à l'application :**
   L'application sera accessible en ligne à l'adresse `http://<IP_DE_VOTRE_VPS>:8501`.
   *(Pensez à ouvrir le port `8501` dans le pare-feu de votre console Ionos)*.

