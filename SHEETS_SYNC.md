# Google Sheets write-back setup

When this is wired up, every edit you make in the app (priority, strategic
tier, type, focus areas, notes) is mirrored to the corresponding row in
your Google Sheet within ~1 second. The Sheet stays the single source of
truth — read by the app on load, written by the app on edit.

Setup is one-time, ~10 minutes.

## 1. Get your data into Google Sheets

If your CSV is currently local, upload it once:

1. <https://drive.google.com> → **New** → **File upload** → pick your CSV
2. Once uploaded, right-click the file → **Open with** → **Google Sheets**
3. Save (auto-saves as a Sheet)

## 2. Add an `id` column if your sheet doesn't have one

The write-back needs to find the right row by stable ID.

1. In the Sheet, insert a column at **A** (right-click column A → Insert 1 left)
2. Name it `id` (header row, cell A1)
3. In cell A2, paste this formula and drag down:
   ```
   =ARRAYFORMULA(IF(B2:B="", "", "e" & TEXT(ROW(B2:B)-1, "0000")))
   ```
   This auto-generates `e0001`, `e0002`, … matching the app's ID scheme.
4. Optional but recommended: copy column A and paste-as-values over itself
   (Edit → Paste special → Values only), so the IDs don't shift if rows are reordered.

## 3. Publish the sheet as CSV (so the app can read it)

1. In the Sheet: **File** → **Share** → **Publish to web**
2. Tab: **Link**. Pick the sheet that has your data + format **Comma-separated values (.csv)**
3. Click **Publish** → copy the URL (looks like `https://docs.google.com/spreadsheets/d/e/.../pub?output=csv`)
4. In the app: **Settings** (left sidebar) → paste this URL → **Save & Load**
5. Confirm the entity list still loads correctly

## 4. Add the write-back Apps Script

1. In the Sheet: **Extensions** → **Apps Script**
2. Delete the default `function myFunction()...` content
3. Paste the entire script below
4. Click **Save** (disk icon), name the project `H-Tracker write-back`

```javascript
/**
 * H-FARM Tracker write-back receiver.
 * Deploy this as Web App: Execute as Me, Access: Anyone with link.
 * Accepts POST {entity_id, field, value} and updates the matching row.
 */
function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');

    // NEW: `op: append` creates a new row with the supplied fields dict.
    // Used by the Prospect Discovery flow when a user approves an
    // AI-discovered partner — adds it to the Sheet with full provenance
    // so the team sees it just like a human-entered partner.
    if (String(body.op || '').toLowerCase() === 'append') {
      return _handleAppend(body);
    }

    // Existing: single-cell update by entity_id + field
    const entityId = String(body.entity_id || '').trim();
    const field = String(body.field || '').trim();
    if (!entityId || !field) return _json({ok: false, error: 'entity_id and field are required'});

    const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    const idCol = headers.indexOf('id') + 1;
    const fieldCol = headers.indexOf(field) + 1;
    if (idCol === 0)    return _json({ok: false, error: "Sheet has no 'id' column"});
    if (fieldCol === 0) return _json({ok: false, error: "Sheet has no '" + field + "' column"});

    const lastRow = sheet.getLastRow();
    if (lastRow < 2) return _json({ok: false, error: 'Sheet is empty'});

    const ids = sheet.getRange(2, idCol, lastRow - 1, 1).getValues();
    let rowNum = -1;
    for (let i = 0; i < ids.length; i++) {
      if (String(ids[i][0]).trim() === entityId) { rowNum = i + 2; break; }
    }
    if (rowNum === -1) return _json({ok: false, error: 'Entity not found: ' + entityId});

    sheet.getRange(rowNum, fieldCol).setValue(body.value);
    return _json({ok: true, row: rowNum, col: fieldCol, field: field});
  } catch (err) {
    return _json({ok: false, error: 'Apps Script error: ' + err.message});
  }
}

/**
 * Append a new row. Body shape: {op: 'append', entity_id: 'ent-xxx', fields: {col: val, ...}}.
 * Only writes columns whose headers exist in the sheet — unknown keys are silently skipped,
 * so the server can over-supply optional columns like `source` / `source_user` / `source_date`
 * that some sheets may not have.
 *
 * Idempotency: if a row with the given entity_id already exists, returns ok:false so the
 * caller can decide. (Server-side dedupe is the primary defense; this is the safety net.)
 */
function _handleAppend(body) {
  const entityId = String(body.entity_id || '').trim();
  const fields = body.fields || {};
  if (!entityId) return _json({ok: false, error: "append requires entity_id"});

  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const idCol = headers.indexOf('id') + 1;
  if (idCol === 0) return _json({ok: false, error: "Sheet has no 'id' column"});

  // Dedupe: scan existing IDs
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    const ids = sheet.getRange(2, idCol, lastRow - 1, 1).getValues();
    for (let i = 0; i < ids.length; i++) {
      if (String(ids[i][0]).trim() === entityId) {
        return _json({ok: false, error: "Row with id '" + entityId + "' already exists at row " + (i + 2), already_exists: true, row: i + 2});
      }
    }
  }

  // Build the row in header order. Unknown columns in `fields` are skipped
  // (filtered to headers); missing columns become empty strings.
  const filled = [];
  const newRow = headers.map(function(h) {
    var v = fields[h];
    if (v === undefined || v === null) return '';
    filled.push(h);
    return v;
  });

  sheet.appendRow(newRow);
  return _json({
    ok: true,
    op: 'append',
    row: sheet.getLastRow(),
    columns_filled: filled,
    columns_skipped_in_payload: Object.keys(fields).filter(function(k) { return headers.indexOf(k) === -1; })
  });
}

function doGet() {
  return _json({ok: true, message: 'H-Tracker writeback endpoint is alive'});
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
```

### Optional: add a `source` column to your Sheet

The append operation will populate a `source` / `source_user` / `source_date`
column ONLY if those columns exist in your Sheet. If they do, you get a
filterable "where did this row come from?" column — useful when you want
to filter to all AI-discovered partners. Headers to add:

| Header        | What goes there                                    |
|---------------|----------------------------------------------------|
| `source`      | `AI-discovered (Prospect Discovery)` for AI rows; empty for human rows |
| `source_user` | Display name of the user who approved the AI find  |
| `source_date` | YYYY-MM-DD when the approval happened              |

If you skip this, no problem — the same info is also embedded in the
`notes` column with a clear "🤖 AI-DISCOVERED" header so anyone reading
the row sees provenance.

## 5. Deploy as Web App

1. In Apps Script: top right → **Deploy** → **New deployment**
2. Click the gear icon next to "Select type" → pick **Web app**
3. Settings:
   * **Description:** `H-Tracker writeback`
   * **Execute as:** **Me** (your Google account)
   * **Who has access:** **Anyone with the link**
4. Click **Deploy**
5. **Authorize access** when prompted. You'll see a warning that the app
   "isn't verified" — click **Advanced** → **Go to (project name) (unsafe)**.
   This is YOUR script you just pasted; the warning is Google's default
   for any non-published script.
6. After authorization, the dialog shows the **Web App URL**. Copy it.

It looks like:
```
https://script.google.com/macros/s/AKfycbxxx...xxx/exec
```

## 6. Add the URL to Render

1. <https://dashboard.render.com> → `h-tracker-api` → **Environment**
2. **Add Environment Variable**:
   * Key: `HFARM_SHEETS_WRITEBACK_URL`
   * Value: paste the Web App URL from step 5
3. **Save Changes** → Render redeploys (~30 s)

## 7. Verify

1. Hard refresh the app: <https://h-tracker-blue.vercel.app/> + Cmd+Shift+R
2. Open any entity → change Priority from one value to another
3. You should see a green toast `✓ Synced to Google Sheets`
4. In your Sheet, the corresponding row's priority cell updates within ~1 second

If you see `⚠ Sheets sync failed`, the toast message shows the reason
(usually a missing column or the entity_id not being in the sheet).

## What's editable + synced

Currently the inline edit form covers:
* `priority`
* `strategic_tier`
* `type`
* `focus_areas`
* `notes`

The Apps Script writes to whatever column matches the field name. If your
sheet uses different column names (`Priority` instead of `priority`, etc.),
you can either rename the columns OR adapt the headers lookup in the
Apps Script to lower-case the comparison.

## Re-deployments

When you edit the Apps Script, you need to deploy a NEW version OR overwrite
the current deployment for changes to take effect:
* **Deploy** → **Manage deployments** → click pencil icon → **Version: New version** → **Deploy**

The Web App URL stays the same across re-deployments.

## Troubleshooting

* **"Entity not found"** — make sure the `id` column in the sheet has the
  exact same IDs the app uses. Check by opening any entity in the app
  and noting its row's id in the Sheet.
* **"Sheet has no 'priority' column"** — your sheet's header doesn't match.
  Rename the column to lowercase `priority`, or adjust the script.
* **All edits silently fail (no toast)** — `HFARM_SHEETS_WRITEBACK_URL`
  isn't set on Render, so the backend doesn't try. Check
  `/api/health` — should show `"sheets_writeback": true`.
* **CORS error in browser console** — the request goes through Flask, not
  directly to Apps Script, so CORS shouldn't be a problem. If you see one,
  paste the error and we'll debug.
