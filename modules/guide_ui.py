"""Bilingual, business-user help for the Ontology Studio."""
from __future__ import annotations

from textwrap import dedent

import streamlit as st


_GUIDE = {
    "English": [
        (
            "🌟 What does the Studio do?",
            """Ontology Studio helps your team agree on what its data means. It reads tables, suggests how they
            connect, and helps you describe business concepts such as customers, sales and revenue.

            The result can be an editable model, Apache Ossie YAML, Databricks metric views, or a model saved
            beside its data in Unity Catalog. You decide what to keep; review suggestions before relying on them.""",
        ),
        (
            "🧭 Which path should I choose?",
            """Use the three choices at the top of the page:

            - **📂 Update existing model**: open a model from the version store, upload an Ossie YAML/JSON file,
              or paste YAML. Check the report, then load it to edit.
            - **✨ Create new model**: connect tables or a file, analyze them, and edit the model.
            - **🚀 Deploy to Databricks**: preview and publish a model to Unity Catalog.

            Your open model stays available when you move between these choices. Need help while working?
            Open **❓ Help & FAQ** above the Ontology Studio logo in the left sidebar.""",
        ),
        (
            "🔌 How do I connect my data?",
            """First choose a source type in the sidebar:

            - **Databricks → Unity Catalog**: choose a catalog, schemas and up to 100 tables or views you can
              access. The app uses your permissions.
            - **Non-Databricks → Upload file**: upload a SQLite database (`.db`, `.sqlite`, `.sqlite3`) or a SQL
              script (`.sql`, `.ddl`, `.txt`). Choose the script's SQL dialect if needed.
            - **Ossie YAML**, **Saved version** or **Paste YAML**: use these when updating a model.

            Snowflake, PostgreSQL and Informatica CDGC are shown as planned sources, not working connectors yet.
            Unity Catalog access is available only when the app runs in Databricks Apps.""",
        ),
        (
            "🔍 What happens during analysis?",
            """Choose **Analyze & build** to read the selected structures and, when enabled, profile data such as
            row counts, distinct values and samples. The Studio looks for keys and possible joins, and classifies
            tables as facts, dimensions or entities and columns as measures, keys, time or descriptive data.

            Strong join candidates may be added automatically. Less certain candidates appear as suggestions;
            inspect their columns and evidence in the **🕸️ Network** tab before accepting them. Optional query
            history and lineage need Databricks and the relevant permissions. Optional AI enrichment can draft
            descriptions, synonyms, meanings, rules and metrics from documents you provide; review every draft.""",
        ),
        (
            "🛠️ How do I edit and check the model?",
            """In **🕸️ Network**, select a class or relationship to edit it in the inspector. Drag classes to
            arrange them. Use **＋ Connect** to add a join; accept or reject suggested joins after checking the
            evidence. The diagram help text explains zoom, layout and keyboard shortcuts.

            Prefer forms? **✏️ YAML form editor** guides you through model information, datasets, fields,
            relationships and metrics. **🧾 Ontology YAML** is for users comfortable editing YAML; apply changes
            to update the diagram and read any validation notes first.

            **🗂️ Tables** lists classes, columns and joins. On Unity Catalog models, **Re-scan** can find schema
            changes. **📘 Data dictionary** explains tables, columns, relationships and measures for Power BI.
            **Take it to Power BI** downloads a starter kit with documentation, CSV files, DAX measures and
            Power Query scripts.""",
        ),
        (
            "📚 How do I add business meaning and governance?",
            """Add clear descriptions, synonyms, column roles and metrics in the model. A good table description
            explains what one row represents. Use **🏠 Domains** to group related tables and metrics, then assign
            owners and statuses: draft, certified or deprecated.

            In **🧠 Semantic layer**, maintain coded-value meanings, business rules, metrics and the glossary.
            In **📚 Knowledge**, write pages that explain business concepts or learn useful joins, filters and
            calculations from a query log. **✅ Review queue** and **🧭 Modeling worklist** help find definitions
            that need attention. Certification is a human decision; check a definition before certifying it.""",
        ),
        (
            "💬 How do I ask a question or check SQL?",
            """In **💬 Ask (Text-to-SQL)**, ask a plain-language question such as “What was net revenue by month?”
            Choose a domain to narrow the context. The app checks whether the model covers the question, writes
            read-only SQL, checks it against the model and data, then shows the query and result. Use
            **Compare with schema-only** to compare a query that does not use the semantic model.

            AI-powered Ask is available in Databricks Apps with a configured model endpoint. Other modes can
            check SQL, show a modeled join path, look up values and manage verified examples. A check helps catch
            common mistakes, but always confirm that the result answers your business question. A 👍 can save a
            useful answer as a verified example.""",
        ),
        (
            "📦 How do I save, export or restore work?",
            """Download **Ontology YAML** for the Studio model, or use **🧩 Apache Ossie** to validate and export
            the portable semantic model as YAML or JSON. **📐 Metric views** can generate views from fact tables;
            you can download the YAML or SQL, or deploy a view when the data is in Unity Catalog.

            To keep version history, the app must be configured with an `ONTOLOGY_STORE` Unity Catalog Volume.
            Use **💾 Save as new version** or **💾 Publish to the Volume store**. Each save creates a new version;
            it does not overwrite the old one. In **🕘 Versions**, compare versions, open one, download it or
            restore it as a new latest version.""",
        ),
        (
            "🚀 How do I deploy a model?",
            """Choose **🚀 Deploy to Databricks**, then select the open model, a saved version, or Ossie YAML.
            Read the validation report and fill in the model name, catalog, schema and volume.

            Review the preview before choosing **🚀 Deploy**. It lists files and SQL statements, including the
            volume creation, comments, tags, metric views, app file-access grant or informational keys you
            selected. Nothing is written before you deploy.
            Deployment needs Databricks Apps and the required Unity Catalog permissions. A failed optional SQL
            step is reported with its error; check each result rather than assuming every step succeeded.""",
        ),
        (
            "📈 What does Monitor show?",
            """**📈 Monitor** lists Ask activity, status, feedback, rows, model time, SQL time, token use and
            estimated cost. Open a request to see its steps and SQL. You can set a daily token budget for Ask.

            Traces are kept for the current app session unless an administrator configures `TELEMETRY_TABLE` to
            store them in Unity Catalog. Loading warehouse cost or saved traces needs Databricks and additional
            permissions. Cost is an estimate based on the token prices shown in Monitor.""",
        ),
        (
            "🧰 What if something is missing or fails?",
            """- **No Unity Catalog source?** Open the app in Databricks Apps and ask an administrator to grant
              the access listed in **Permissions** in the project README.
            - **Cannot save versions?** Version history needs `ONTOLOGY_STORE` and read/write access to its Volume.
              You can still download the model.
            - **Validation reports errors?** Follow the report's file location and suggested fix. Do not deploy a
              model with errors.
            - **Join looks wrong?** Open it in the Network inspector and check the columns and direction. Reject
              an incorrect suggestion or edit the relationship.
            - **Deploy step failed?** Read the per-step error and ask the owner of the catalog, schema or volume
              to grant the specific permission you need.

            The Studio honors your data access. Ask your workspace administrator for missing permissions; do not
            paste passwords, access tokens or other secrets into the app.""",
        ),
    ],
    "Français": [
        (
            "🌟 À quoi sert le Studio ?",
            """Ontology Studio aide votre équipe à donner un sens commun aux données. Il lit les tables,
            propose leurs liens et vous aide à décrire des notions métier comme les clients, les ventes et le
            chiffre d'affaires.

            Vous pouvez créer un modèle modifiable, exporter du YAML Apache Ossie ou des vues métriques
            Databricks, ou enregistrer un modèle près de ses données dans Unity Catalog. Vous choisissez ce que
            vous gardez : vérifiez les suggestions avant de vous y fier.""",
        ),
        (
            "🧭 Quel parcours choisir ?",
            """Utilisez les trois choix en haut de la page :

            - **📂 Update existing model** (mettre à jour) : ouvrez un modèle enregistré, importez un fichier
              Ossie YAML/JSON
              ou collez du YAML. Consultez le rapport, puis chargez le modèle pour le modifier.
            - **✨ Create new model** (créer) : connectez des tables ou un fichier, analysez-les et modifiez
              le modèle.
            - **🚀 Deploy to Databricks** (déployer) : vérifiez l'aperçu et publiez le modèle dans Unity Catalog.

            Le modèle ouvert reste disponible lorsque vous changez de parcours. Besoin d'aide pendant le travail ?
            Ouvrez **❓ Help & FAQ / Aide et FAQ**, au-dessus du logo Ontology Studio dans la barre latérale
            gauche. Les noms des boutons et des onglets sont affichés en anglais ; ils sont indiqués ci-dessous
            en gras pour vous aider à les retrouver.""",
        ),
        (
            "🔌 Comment connecter mes données ?",
            """Choisissez d'abord le type de source dans la barre latérale :

            - **Databricks → Unity Catalog** : choisissez un catalogue, des schémas et jusqu'à 100 tables ou vues
              auxquelles vous avez accès. L'application utilise vos permissions.
            - **Non-Databricks → Upload file** (importer un fichier) : importez une base SQLite
              (`.db`, `.sqlite`, `.sqlite3`)
              ou un script SQL (`.sql`, `.ddl`, `.txt`). Choisissez le dialecte SQL du script si nécessaire.
            - **Ossie YAML**, **Saved version** (version enregistrée) ou **Paste YAML** (coller du YAML) :
              utilisez ces options pour modifier un modèle existant.

            Snowflake, PostgreSQL et Informatica CDGC sont des sources prévues, mais pas encore disponibles.
            Unity Catalog est accessible uniquement lorsque l'application fonctionne dans Databricks Apps.""",
        ),
        (
            "🔍 Que fait l'analyse ?",
            """Cliquez sur **Analyze & build** (analyser et créer) pour lire les structures sélectionnées et, si l'option est
            activée, calculer des profils : nombre de lignes, valeurs distinctes et exemples. Le Studio cherche
            les clés et les liens possibles. Il classe aussi les tables (faits, dimensions ou entités) et les
            colonnes (mesures, clés, dates ou descriptions).

            Les liens très probables peuvent être ajoutés automatiquement. Les autres deviennent des
            suggestions : vérifiez leurs colonnes et les indices dans l'onglet **🕸️ Network** avant de les
            accepter. L'historique des requêtes et la lignée demandent Databricks et les permissions nécessaires.
            L'IA peut proposer des descriptions, synonymes, significations, règles et mesures à partir de vos
            documents ; relisez chaque proposition.""",
        ),
        (
            "🛠️ Comment modifier et vérifier le modèle ?",
            """Dans **🕸️ Network** (réseau), sélectionnez une classe ou un lien pour le modifier dans le panneau de droite.
            Déplacez les classes pour organiser le schéma. Utilisez **＋ Connect** (connecter) pour ajouter un lien ;
            vérifiez les indices avant d'accepter une suggestion. L'aide du schéma explique le zoom, les
            dispositions et les raccourcis clavier.

            Vous préférez des formulaires ? **✏️ YAML form editor** vous guide sur les informations du modèle,
            les jeux de données, les champs, les liens et les mesures. **🧾 Ontology YAML** permet de
            modifier directement le YAML ; appliquez les changements et lisez les avertissements de validation.

            **🗂️ Tables** liste les classes, colonnes et liens. Pour Unity Catalog, **Re-scan** (analyser à nouveau)
            détecte les changements de schéma. **📘 Data dictionary** explique les tables, colonnes, relations et
            mesures pour Power BI. **Take it to Power BI** télécharge un kit de démarrage avec la documentation,
            des fichiers CSV, des mesures DAX et des scripts Power Query.""",
        ),
        (
            "📚 Comment ajouter le sens métier et la gouvernance ?",
            """Ajoutez des descriptions claires, synonymes, rôles de colonnes et mesures. Une bonne description
            de table explique ce que représente une ligne. Utilisez **🏠 Domaines** pour regrouper les tables et
            mesures, puis indiquez les responsables et les statuts : brouillon, certifié ou obsolète.

            Dans **🧠 Semantic layer**, gérez les significations des codes, règles métier, mesures et le
            glossaire. Dans **📚 Knowledge**, créez des pages qui expliquent les notions métier ou apprenez des
            liens, filtres et calculs à partir d'un journal de requêtes. **✅ Review queue** (file de révision) et
            **🧭 Modeling worklist** indiquent ce qui doit être vérifié. La certification est une décision humaine :
            vérifiez la définition avant de la certifier.""",
        ),
        (
            "💬 Comment poser une question ou vérifier du SQL ?",
            """Dans **💬 Ask (Text-to-SQL)**, écrivez une question simple, par exemple « Quel était le
            chiffre d'affaires net par mois ? ». Choisissez un domaine pour limiter le contexte. L'application
            vérifie si le modèle comprend la question, crée une requête SQL en lecture seule, la contrôle par
            rapport au modèle et aux données, puis affiche la requête et le résultat. **Compare with schema-only**
            (comparer au schéma seul) permet de comparer avec une requête qui n'utilise pas le modèle sémantique.

            La fonction IA de questions nécessite Databricks Apps et un point de terminaison configuré. Les modes
            **Check SQL**, **Join path**, **Look up values** et **Verified examples** vérifient du SQL, affichent
            un chemin de jointure, cherchent des valeurs ou gèrent des exemples vérifiés. Ces contrôles aident à
            repérer des erreurs courantes, mais vérifiez toujours que
            le résultat répond à la question métier. Un 👍 peut enregistrer une bonne réponse comme exemple.""",
        ),
        (
            "📦 Comment enregistrer, exporter ou restaurer le travail ?",
            """Téléchargez **Ontology YAML** pour le modèle du Studio, ou utilisez **🧩 Apache Ossie** pour
            valider et exporter le modèle sémantique en YAML ou JSON. **📐 Metric views** peut créer une vue par
            table de faits. Dans **➕ New metric view**, suivez les étapes pour choisir les liens, champs, mesures
            et filtres, puis vérifiez le résultat. Téléchargez le YAML ou le SQL, ou déployez une vue si les
            données sont dans Unity Catalog.

            Pour conserver l'historique, l'application doit être configurée avec un volume Unity Catalog
            `ONTOLOGY_STORE`. Utilisez **💾 Save as new version** ou **💾 Publish to the Volume store**.
            Chaque enregistrement crée une nouvelle version, sans écraser l'ancienne. Dans **🕘 Versions**,
            comparez, ouvrez, téléchargez ou restaurez une version comme nouvelle version la plus récente.""",
        ),
        (
            "🚀 Comment déployer un modèle ?",
            """Choisissez **🚀 Deploy to Databricks**, puis sélectionnez le modèle ouvert, une version
            enregistrée ou un fichier Ossie YAML. Lisez le rapport de validation et indiquez le nom du modèle,
            le catalogue, le schéma et le volume.

            Vérifiez l'aperçu avant de cliquer sur **🚀 Deploy**. Il liste les fichiers et les instructions
            SQL : création du volume, commentaires, étiquettes, vues métriques, accès fichiers pour l'application
            ou clés informatives selon vos choix. Aucune écriture n'a lieu avant le déploiement. Il faut
            Databricks Apps et les permissions
            Unity Catalog nécessaires. Une étape SQL en échec affiche son erreur ; vérifiez chaque résultat.""",
        ),
        (
            "📈 Que montre Monitor ?",
            """**📈 Monitor** liste l'activité Ask, le statut, les retours, les lignes, les durées, les jetons
            utilisés et le coût estimé. Ouvrez une demande pour voir ses étapes et son SQL. Vous pouvez définir
            un budget quotidien de jetons pour Ask.

            Les traces restent dans la session de l'application, sauf si un administrateur configure
            `TELEMETRY_TABLE` pour les enregistrer dans Unity Catalog. L'accès aux coûts du warehouse et aux
            traces enregistrées demande Databricks et des permissions supplémentaires. Le coût est une estimation
            basée sur les prix par jeton affichés dans Monitor.""",
        ),
        (
            "🧰 Que faire si une option manque ou échoue ?",
            """- **Pas de source Unity Catalog ?** Ouvrez l'application dans Databricks Apps et demandez à
              l'administrateur les accès indiqués dans la section **Permissions** du README du projet.
            - **Impossible d'enregistrer une version ?** Il faut configurer `ONTOLOGY_STORE` et avoir un accès
              en lecture et écriture au volume. Vous pouvez toujours télécharger le modèle.
            - **Des erreurs de validation ?** Suivez l'emplacement du problème et la correction proposée dans le
              rapport. Ne déployez pas un modèle en erreur.
            - **Un lien semble incorrect ?** Vérifiez les colonnes et le sens dans le panneau du réseau. Rejetez
              la suggestion incorrecte ou modifiez le lien.
            - **Une étape du déploiement échoue ?** Lisez son erreur et demandez au responsable du catalogue, du
              schéma ou du volume la permission correspondante.

            Le Studio respecte vos accès aux données. Demandez les permissions manquantes à l'administrateur ;
            ne collez pas de mot de passe, jeton d'accès ou autre secret dans l'application.""",
        ),
    ],
}


@st.dialog("📘 Help & FAQ | Aide et FAQ", width="large")
def _show_guide() -> None:
    language = st.radio(
        "Language / Langue",
        options=list(_GUIDE),
        horizontal=True,
        key="studio_guide_language",
        label_visibility="collapsed",
    )
    if language == "English":
        st.caption("Choose a topic to see simple instructions for using Ontology Studio.")
    else:
        st.caption("Choisissez un sujet pour obtenir des instructions simples sur Ontology Studio.")

    for index, (title, content) in enumerate(_GUIDE[language]):
        with st.expander(title, expanded=index == 0):
            st.markdown(dedent(content))


def sidebar_button() -> None:
    """Render the always-available help entry at the top of the app sidebar."""
    if st.button("❓ Help & FAQ / Aide et FAQ", key="studio_guide_button", use_container_width=True):
        _show_guide()
