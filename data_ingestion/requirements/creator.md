### 14. Duplicate script <custom data-type="status" data-id="id-31">phase 2</custom>  <custom data-type="status" data-id="id-32">ready</custom>

1. **User story**: As a Reviewer, I want to duplicate an existing script and apply it to a chosen product, so that I can quickly create a new script from an existing one without rebuilding it from scratch.
2. **Acceptance Criteria**

**AC1: Entry point**

* "Duplicate" action available per script (Scripts tab → Action column, and/or Script Detail header).
* Available for scripts in any status (DRAFT or PUBLISHED). Duplicating always produces a **new DRAFT** and never modifies the original.
* Click "Duplicate" → open "Duplicate script" modal.

**AC2: Duplicate script modal — field specification**

* Header: "Duplicate script" + ✕ close (click → dismiss without creating).

| Field | Description | Data type | Format | Required? | Validation on field |
| --- | --- | --- | --- | --- | --- |
| Product | Product to apply to the new script | string (productId) | Searchable single-select dropdown; placeholder "Select product" | Yes | Default = current script's product; Options = list of products (search by product name, label/ ; on Save if empty → inline error "Please select a product" **filter by product line of original script** |

* **Cancel**: dismiss modal, no copy created.
* **Save**: always enabled; on click → server validates

**AC3: Save system behavior (create draft copy)**

On Save, system creates a new script with:

1. New script ID = `SC` + incremental number
2. Script name = `%Original name%_Copy`
3. `status` = `Draft`; `AI gen` = `false`
4. **Product information** (productId, productName, product line, variant catalog, description, USP) = from the **selected product**
5. **Important — Read me first** = copied from the original script
6. **Scene requirements** = copied from the original (title, action, background, detail requirement, fee, references), **with per-scene Product variants = not selected (empty)**
7. **Persona** = copied from the original script
8. `createdBy` = CURRENT_REVIEWER.id; `createdAt`, `updatedAt` = now(); `deadline` = today + 30 days, `script type` copied from the original script
9. Navigate to the new Draft Script Detail screen
10. Success toast: "Duplicate script successfully!"
11. Create failed → toast "Failed to duplicate script! Please try again" (modal stays open).
12. **Corner Case**

* **CC1:** Product search returns no match → dropdown empty state "No product found."
* **CC3:** Network / system error → show toastbar error message.