# Kanban board (simple)

Build a small, single-user kanban web app for tracking personal tasks. No accounts, no backend persistence beyond what is reasonable for a demo (e.g. in-memory or browser `localStorage`).

## Columns

Default columns: **To Do**, **In Progress**, **Done**. User can rename columns but not delete below three columns.

## Cards

- Each card has a **title** (required) and optional **description**.
- Cards live in exactly one column at a time.
- User can **add**, **edit**, and **delete** cards.
- User can **move** a card to another column (drag-and-drop preferred; buttons are acceptable if DnD is too heavy).

## Board behavior

- New cards are added to **To Do** unless the user picks another column when creating.
- Column order is fixed left-to-right as above; card order within a column is user-controlled (reorder via drag or explicit move up/down).
- Empty columns show a short placeholder (e.g. “No cards”).

## UI

- Clean, readable layout: columns side-by-side on desktop; stacked or horizontally scrollable on narrow screens.
- Obvious primary actions: add card, edit, delete, move.
- No login screen.

## Out of scope

- Multi-user collaboration, real-time sync, attachments, due dates, labels/tags, search/filter, notifications, mobile native apps, or production-grade auth.

## Done when

- A user can create a board with the three columns, manage cards across columns, and refresh the page without losing data (if persistence is implemented).
- Basic manual test path is documented in a short README (how to run and what to click).
