---
template: home.html
title: "Ava: a private AI assistant that runs on your own computer"
description: A private AI hub that runs on the box you already own, and tells you which parts of that claim it has verified on your hardware.
hide:
  - navigation
  - toc
---

<!-- INTENTIONALLY EMPTY, and it has to keep existing.

     sync.py declares HOME_PAGE = ("home.md", "index.md") and appends to
     `missing` if this file is gone, so deleting it would warn and still exit 0
     while the homepage vanished. The front matter above is the whole job:
     `template:` points at the page, `title:` and `description:` feed <title>
     and the Open Graph tags that overrides/main.html builds from page.meta.

     The body is empty because the landing page IS overrides/home.html. It used
     to carry a second, worse copy of the Quickstart here - an install fence,
     two ??? notes, a profile table and a root-Docker-socket warning - and
     Material wraps whatever lands in this body in a typeset <article> that can
     never be full-bleed, which is what broke the page's axis halfway down.
     Every one of those blocks already lives in deploy/README.md,
     docs/INSTALL_REFERENCE.md, docs/AGENT_RUNTIME.md and
     docs/capabilities/agent.md, so nothing was moved, only dropped.

     Adding markdown back here does not add a section to the landing page: the
     template empties {% block container %}, so it would render nowhere. Put
     new bands in overrides/home.html.

     One consequence, accepted knowingly: MkDocs' search plugin indexes
     page.content, so the homepage is now an empty search record. Nobody
     searches a site for its homepage, it is reached from the header mark, and
     every claim on it is duplicated on a page that IS indexed. -->
