# Déployer l'app depuis un iPhone (sans ordinateur, sans terminal)

Ce dossier contient 3 fichiers qui vont ensemble : `app.py`, `requirements.txt`,
`index.html`. L'idée : au lieu de faire tourner Python sur votre iPhone (pas
possible), on héberge ces 3 fichiers gratuitement sur un serveur en ligne
(Render). Une fois déployé, vous n'avez plus qu'**une seule adresse à ouvrir
dans Safari**, l'app et ses données en direct fonctionnent dessus.

Tout se fait au doigt, dans Safari. Ça prend 10-15 minutes la première fois.

## Étape 1 — Récupérer les 3 fichiers sur votre iPhone

Dans ce chat, téléchargez `app.py`, `requirements.txt` et `index.html`
(bouton de téléchargement sur chaque fichier). Ils atterrissent normalement
dans l'app **Fichiers** de l'iPhone (dossier "Téléchargements" ou "iCloud
Drive"). Gardez-les tous les trois dans le même dossier.

## Étape 2 — Créer un compte GitHub (gratuit)

1. Ouvrez Safari, allez sur **github.com**, appuyez sur "Sign up".
2. Créez un compte gratuit (email + mot de passe).

## Étape 3 — Créer un nouveau "repository" et y déposer les fichiers

1. Une fois connecté, appuyez sur le **+** en haut à droite → "New repository".
2. Donnez-lui un nom, par exemple `etf-pea-optimizer`. Laissez-le "Public".
   Appuyez sur "Create repository".
3. Sur la page du repository, cherchez le bouton **"Add file" → "Upload
   files"**.
4. Appuyez sur la zone d'upload : ça ouvre le sélecteur de fichiers de
   l'iPhone → allez dans **Fichiers** → sélectionnez les 3 fichiers
   (`app.py`, `requirements.txt`, `index.html`) → "Ajouter".
5. Tout en bas de la page, appuyez sur **"Commit changes"** (le bouton vert).

Vos 3 fichiers sont maintenant en ligne sur GitHub (ce n'est pas encore un
site qui fonctionne, juste un dépôt de fichiers — l'étape suivante les fait
tourner réellement).

## Étape 4 — Créer un compte Render (gratuit) et déployer

1. Toujours dans Safari, allez sur **render.com** → "Get Started" → créez un
   compte (le plus simple : "Sign up with GitHub", ça relie directement les
   deux comptes).
2. Une fois connecté, appuyez sur **"New +"** → **"Web Service"**.
3. Choisissez **"Build and deploy from a Git repository"**, puis
   sélectionnez le repository `etf-pea-optimizer` créé à l'étape 3.
4. Render propose un formulaire de configuration — remplissez (ou vérifiez)
   ces champs :
   - **Name** : ce que vous voulez, ex. `etf-pea-optimizer`
   - **Language/Environment** : `Python 3`
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type / Plan** : choisissez **Free**
5. Appuyez sur **"Create Web Service"** (ou "Deploy Web Service").
6. Render installe les dépendances et démarre le serveur — ça prend
   généralement 2 à 5 minutes la première fois. Vous voyez défiler des logs ;
   attendez le message indiquant que le service est "Live".

## Étape 5 — Utiliser l'app

En haut de la page Render de votre service, une URL du type
`https://etf-pea-optimizer-xxxx.onrender.com` est affichée. Ouvrez-la dans
Safari : l'application s'affiche directement (interface + backend au même
endroit, le champ "Backend" peut rester vide). Ajoutez-la à votre écran
d'accueil (bouton Partager → "Sur l'écran d'accueil") pour y accéder comme
une app.

## À savoir sur le plan gratuit de Render

- Après **15 minutes sans visite**, le service gratuit s'endort
  automatiquement. La prochaine fois que vous ouvrez le lien, la première
  réponse peut prendre **30 à 60 secondes** ("cold start") — c'est normal,
  pas un bug. L'app est programmée pour attendre jusqu'à 65 secondes avant
  d'afficher une erreur.
- Si vous modifiez `app.py` ou `index.html` plus tard, remettez-les à jour
  sur GitHub (même bouton "Upload files", en remplaçant les fichiers) :
  Render redéploie automatiquement à chaque changement du repository.

## Si quelque chose ne fonctionne pas

- Sur la page de votre service Render, l'onglet **"Logs"** montre les
  erreurs éventuelles (utile même sans être développeur : cherchez une ligne
  contenant "Error" ou "Traceback").
- Vérifiez que les 3 fichiers sont bien tous les trois à la racine du
  repository GitHub (pas dans un sous-dossier), sinon `app.py` ne retrouve
  pas `index.html`.
