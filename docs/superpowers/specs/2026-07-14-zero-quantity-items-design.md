# Zero-Quantity Postal Items

## Goal

Treat an order content row with quantity less than or equal to zero as an item that is not included in the current shipment. This supports partial shipment without submitting an invalid zero-quantity item to Japan Post.

## Confirmed Behavior

- Keep zero-quantity rows visible and editable in the Streamlit pending-order UI.
- Do not clear or rewrite the original content, value, quantity, or HS Code fields.
- Exclude zero-quantity rows from Japan Post content-item Confirm requests.
- Exclude zero-quantity rows from HS Code preparation and validation.
- Exclude zero-quantity rows from declared USD and JPY totals.
- Apply the same exclusion rule to the requests flow and the Playwright fallback flow.
- Preserve original item indexes for remaining items so manual HS Code values still map to the correct source row.
- If every content row has quantity less than or equal to zero, stop before entering the Japan Post item flow and report a clear error.

## Data Flow

`_iter_content_items` is the shared shipment-item boundary. It will parse quantity first and return only positive-quantity items. Consumers such as batch HS Code preparation and the M060800 requests flow will therefore receive the same filtered item set.

`calculate_total_value_usd` will multiply each value by its parsed quantity directly. A zero quantity contributes zero instead of falling back to one. The existing editor write-back then recalculates JPY from the corrected USD total.

The legacy Playwright loops that read content columns directly will explicitly skip non-positive quantities, matching the shared requests behavior.

## Error Handling

Invalid or blank quantities retain the existing fallback of one to avoid changing established orders. Explicit numeric quantities less than or equal to zero are the only values treated as excluded. When filtering leaves no shippable items, automation raises a descriptive error instead of submitting an empty content form.

## Tests

- `_iter_content_items` skips an earlier zero-quantity row and preserves the later source index.
- `_iter_content_items` excludes negative quantities.
- Declared total excludes zero-quantity rows.
- Declared total remains correct for positive quantities and blank/default quantities.
- All-zero input produces no shippable items and is rejected before M060800 submission.
- Existing automation, editor, UI feedback, and label tests remain green.
