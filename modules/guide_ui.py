"""Bilingual, business-user help for the Ontology Studio."""
from __future__ import annotations

from textwrap import dedent

import streamlit as st


_GUIDE = {
    "English": [
        (
            "🌟 What does the Studio do?",
            """Ontology Studio helps your team agree on what its data means, then puts that meaning where people
            already work in Databricks. It reads your Unity Catalog tables, suggests how they connect, helps you
            describe business concepts (customers, sales, revenue), proves the model with your own questions, and
            publishes it as table comments, tags, metric views and a Genie space. Nothing is copied out of
            Databricks, and everything runs with your own permissions.""",
        ),
        (
            "🧭 The six steps",
            """Work left to right across the buttons at the top of the page:

            1. **Connect**: build a model from Unity Catalog tables, open a saved model, import a file, or start
               from a blank form.
            2. **Model**: check the diagram, fix wrong joins, mark facts and dimensions.
            3. **Describe**: add what only people know: descriptions, code values, glossary, rules, metrics,
               domains, owners and pages.
            4. **Review**: one inbox: blockers, suggested joins, certification, gaps.
            5. **Test**: run your test questions and see what the model answers correctly.
            6. **Publish**: save a version, deploy to Databricks, or download exports.

            The sidebar always shows the open model, whether it has unsaved changes, its readiness and its test
            result. **Advanced mode** in the sidebar shows expert options.""",
        ),
        (
            "🔌 How do I connect my data?",
            """In **Connect**, pick how to start:

            - 🏛️ Unity Catalog tables: choose a catalog, schemas and up to 100 tables or views. Available when
              the app runs in Databricks Apps.
            - 📂 Saved model: open a version from the version store.
            - 📥 Import a file: an Apache Ossie model (YAML or JSON, checked against the official
              specification), a studio model, a SQLite database or a .sql script, or paste YAML.
            - ✏️ Start from scratch: describe a model step by step in plain forms.

            **Options** lets you profile data, learn from query history and lineage, and draft descriptions with
            AI from documents you upload. Review every AI draft before relying on it.""",
        ),
        (
            "🛠️ How do I fix the model?",
            """In **Model → Diagram**, click a table or a join to edit it in the inspector on the right. Drag
            tables to arrange them. Use **＋ Connect** to add a join. Dashed orange lines are suggested joins:
            click one to accept or reject it, or settle them all in **Review → Suggested joins**.

            **List** shows tables and joins as tables and can re-scan Unity Catalog for new columns or tables.
            **Data preview** shows read-only, paginated rows from a modeled table; choose the page size and use
            the previous and next arrows to move through the data. **Form** edits the model step by step without
            YAML.""",
        ),
        (
            "📚 How do I add business meaning?",
            """**Describe** holds everything people know about the data:

            - **Dictionary**: search any table, column, metric or page and read one table in business terms.
            - **Meanings**: what code values mean, the glossary (MRR = monthly recurring revenue), business rules.
            - **Metrics**: the model's metrics and the metric views built from them, plus a guided builder.
            - **Domains**: group tables into Finance, Sales …, assign owners and certify definitions.
            - **Governance clarity**: a donut shows which table and metric descriptions, owners, domains and review
              statuses are recorded. It measures documentation coverage, not governance quality or certification.
            - **Knowledge**: pages that explain concepts, and calculations learned from the queries people run.

            Certification is a human decision; read a definition before you certify it.""",
        ),
        (
            "✅ What does Review show?",
            """One place for everything that needs a decision: **Blockers** (the model would not export as a valid
            Apache Ossie document), **Suggested joins** (accept or reject in bulk), **Queue** (popular calculations
            to promote, definitions to certify, conflicts, feedback hotspots), **Worklist** (tables to describe,
            default joins) and the **Change log**. The readiness line at the top says what to do next.""",
        ),
        (
            "🎯 How do I test the model?",
            """In **Test → Test set**, add the questions your business asks, each with the SQL that answers it.
            **Run** asks every question again through the model (and, if you like, from the raw tables only) and
            compares the results. Run it after each change: **Publish** shows the result, and the questions also
            become the Genie space's sample questions and benchmarks.

            **Ask** answers one question at a time and has tools to check SQL, find join paths and look up values.
            A 👍 adds a good answer to the test set. **Traces** shows every request. AI features need the app to run
            in Databricks Apps with a model endpoint.""",
        ),
        (
            "🚀 How do I save and publish?",
            """**Publish → Save & versions** saves the open model as a new version (an Apache Ossie YAML file in the
            version store; nothing is overwritten). Compare, open or restore any version there.

            **Publish → Deploy** builds one plan for everything the app writes to Databricks: the YAML in a volume
            next to the tables, table and column comments, tags (model, version, domain, certification, owner),
            metric views, informational keys and, if you tick it, a **Genie space**. Read the preview; nothing is
            written until you click 🚀 Deploy, and each step reports its own result.

            **Publish → Exports** downloads Apache Ossie YAML or JSON, the metric views, a Power BI starter kit
            (DAX measures, Power Query scripts) and the data dictionary.""",
        ),
        (
            "🧰 What if something is missing or fails?",
            """- **No Unity Catalog option?** Open the app in Databricks Apps and ask an administrator for the
              access listed under *Permissions* in the README.
            - **Cannot save versions?** The app needs `ONTOLOGY_STORE` and read/write access to its volume. You can
              still download the model in Exports.
            - **Genie space failed?** The app needs the `genie` user authorization scope. Download the space
              definition from the deploy preview and ask an administrator.
            - **A deploy step failed?** Read its error and ask the owner of the catalog, schema or volume for that
              permission.

            The Studio honors your data access. Never paste passwords, access tokens or other secrets into it.""",
        ),
    ],
    "Français": [
        (
            "🌟 À quoi sert le Studio ?",
            """Ontology Studio aide votre équipe à s'accorder sur le sens de ses données, puis place ce sens là où
            les gens travaillent déjà dans Databricks. Il lit vos tables Unity Catalog, propose les liens entre
            elles, aide à décrire les notions métier (clients, ventes, revenus), vérifie le modèle avec vos propres
            questions et le publie sous forme de commentaires, d'étiquettes, de vues de métriques et d'un espace
            Genie. Rien ne sort de Databricks, et tout s'exécute avec vos propres droits.""",
        ),
        (
            "🧭 Les six étapes",
            """Avancez de gauche à droite avec les boutons en haut de la page :

            1. **Connect** (connecter) : créer un modèle à partir de tables Unity Catalog, ouvrir un modèle
               enregistré, importer un fichier ou partir d'un formulaire vide.
            2. **Model** (modèle) : vérifier le schéma, corriger les liens, marquer faits et dimensions.
            3. **Describe** (décrire) : ajouter ce que seules les personnes savent : descriptions, codes, glossaire,
               règles, métriques, domaines, responsables, pages.
            4. **Review** (revue) : une seule boîte de réception : blocages, liens proposés, certification, manques.
            5. **Test** : lancer vos questions de test et voir ce que le modèle répond correctement.
            6. **Publish** (publier) : enregistrer une version, déployer dans Databricks ou télécharger.

            La barre latérale montre toujours le modèle ouvert, les changements non enregistrés, son état et le
            résultat des tests. Le **mode avancé** affiche les options expertes.""",
        ),
        (
            "🔌 Comment connecter mes données ?",
            """Dans **Connect**, choisissez comment commencer :

            - 🏛️ Unity Catalog tables : un catalogue, des schémas et jusqu'à 100 tables ou vues (dans
              Databricks Apps).
            - 📂 Saved model : ouvrir une version du magasin de versions.
            - 📥 Import a file : un modèle Apache Ossie (YAML ou JSON, vérifié selon la spécification
              officielle), un modèle du Studio, une base SQLite ou un script .sql, ou du YAML collé.
            - ✏️ Start from scratch : décrire un modèle pas à pas dans des formulaires.

            **Options** permet de profiler les données, d'apprendre de l'historique des requêtes et du lignage, et
            de rédiger des descriptions avec l'IA à partir de vos documents. Relisez chaque brouillon de l'IA.""",
        ),
        (
            "🛠️ Comment corriger le modèle ?",
            """Dans **Model → Diagram**, cliquez sur une table ou un lien pour le modifier dans le panneau de droite.
            Faites glisser les tables pour les ranger. **＋ Connect** ajoute un lien. Les lignes orange en
            pointillés sont des liens proposés : cliquez pour les accepter ou les rejeter, ou traitez-les tous dans
            **Review → Suggested joins**.

            **List** présente tables et liens sous forme de tableaux et peut relire Unity Catalog pour trouver de
            nouvelles colonnes. **Data preview** affiche les lignes d'une table du modèle en lecture seule et par
            pages ; choisissez la taille de page et utilisez les flèches précédente et suivante. **Form** modifie
            le modèle pas à pas, sans YAML.""",
        ),
        (
            "📚 Comment ajouter le sens métier ?",
            """**Describe** réunit tout ce que l'on sait des données :

            - **Dictionary** : chercher une table, une colonne, une métrique ou une page.
            - **Meanings** : la signification des codes, le glossaire, les règles métier.
            - **Metrics** : les métriques du modèle et leurs vues de métriques, avec un assistant de création.
            - **Domains** : regrouper les tables (Finance, Ventes…), nommer les responsables, certifier.
            - **Governance clarity** : un anneau montre les descriptions, responsables, domaines et statuts de revue
              enregistrés pour les tables et métriques. C'est une couverture documentaire, pas une note de qualité
              ni une certification.
            - **Knowledge** : des pages qui expliquent les notions et les calculs appris des requêtes.

            La certification est une décision humaine : lisez la définition avant de la certifier.""",
        ),
        (
            "✅ Que montre Review ?",
            """Un seul endroit pour tout ce qui demande une décision : **Blockers** (le modèle ne s'exporterait pas
            en document Apache Ossie valide), **Suggested joins** (accepter ou rejeter en lot), **Queue** (calculs
            populaires à promouvoir, définitions à certifier, conflits, retours négatifs), **Worklist** (tables à
            décrire, liens par défaut) et le **Change log** (journal). La ligne d'état en haut indique la suite.""",
        ),
        (
            "🎯 Comment tester le modèle ?",
            """Dans **Test → Test set**, ajoutez les questions de votre métier, chacune avec le SQL qui y répond.
            **Run** repose chaque question au modèle (et, si vous le souhaitez, aux seules tables brutes) et compare
            les résultats. Relancez après chaque changement : **Publish** affiche le résultat, et les questions
            deviennent les exemples et les tests de l'espace Genie.

            **Ask** répond à une question à la fois et propose des outils pour vérifier du SQL, trouver un chemin
            de jointure ou chercher des valeurs. Un 👍 ajoute une bonne réponse aux tests. **Traces** montre chaque
            requête. Les fonctions d'IA demandent Databricks Apps et un point de terminaison de modèle.""",
        ),
        (
            "🚀 Comment enregistrer et publier ?",
            """**Publish → Save & versions** enregistre le modèle ouvert comme nouvelle version (un fichier Apache
            Ossie YAML ; rien n'est écrasé). Comparez, ouvrez ou restaurez une version au même endroit.

            **Publish → Deploy** prépare un seul plan pour tout ce que l'application écrit dans Databricks : le YAML
            dans un volume à côté des tables, les commentaires, les étiquettes (modèle, version, domaine,
            certification, responsable), les vues de métriques, les clés informatives et, si vous le cochez, un
            **espace Genie**. Lisez l'aperçu : rien n'est écrit avant 🚀 Deploy, et chaque étape donne son
            résultat.

            **Publish → Exports** télécharge le modèle Apache Ossie, les vues de métriques, un kit Power BI (mesures
            DAX, scripts Power Query) et le dictionnaire de données.""",
        ),
        (
            "🧰 Que faire si quelque chose manque ou échoue ?",
            """- **Pas d'Unity Catalog ?** Ouvrez l'application dans Databricks Apps et demandez à l'administrateur
              les accès listés dans *Permissions* du README.
            - **Impossible d'enregistrer des versions ?** Il faut `ONTOLOGY_STORE` et l'accès en lecture et écriture
              à son volume. Vous pouvez toujours télécharger le modèle dans Exports.
            - **L'espace Genie a échoué ?** L'application a besoin du droit `genie`. Téléchargez la définition de
              l'espace depuis l'aperçu et transmettez-la à l'administrateur.
            - **Une étape du déploiement échoue ?** Lisez son erreur et demandez la permission au responsable du
              catalogue, du schéma ou du volume.

            Le Studio respecte vos accès. Ne collez jamais de mot de passe, de jeton ou d'autre secret.""",
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
