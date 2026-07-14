# Zero-Quantity Postal Items

## Goal

Treat an order content row with a blank quantity or a quantity less than or equal to zero as an item that is not included in the current shipment. In the source sheet `gid=628056983`, both blank and zero mean that the item was canceled. This supports partial shipment without submitting a canceled item to Japan Post.

## Confirmed Behavior

- Keep zero-quantity rows visible and editable in the Streamlit pending-order UI.
- Do not clear or rewrite the original content, value, quantity, or HS Code fields.
- Exclude blank, zero, and negative-quantity rows from Japan Post content-item Confirm requests.
- Exclude blank, zero, and negative-quantity rows from HS Code preparation and validation.
- Exclude blank, zero, and negative-quantity rows from declared USD and JPY totals.
- Apply the same exclusion rule to the requests flow and the Playwright fallback flow.
- Preserve original item indexes for remaining items so manual HS Code values still map to the correct source row.
- If every content row has quantity less than or equal to zero, stop before entering the Japan Post item flow and report a clear error.

## Data Flow

`_iter_content_items` is the shared shipment-item boundary. It will parse quantity first and return only positive-integer-quantity items. Consumers such as batch HS Code preparation and the M060800 requests flow will therefore receive the same filtered item set.

`calculate_total_value_usd` will multiply each value by its parsed quantity directly. A zero quantity contributes zero instead of falling back to one. The existing editor write-back then recalculates JPY from the corrected USD total.

The legacy Playwright loops that read content columns directly will explicitly skip non-positive quantities, matching the shared requests behavior.

## Error Handling

Blank quantities and numeric quantities less than or equal to zero are treated as excluded. They are never changed to one. Non-numeric or non-integer quantities are rejected with an error that identifies the affected item. When filtering leaves no shippable items, automation raises a descriptive error instead of submitting an empty content form.

## Tests

- `_iter_content_items` skips earlier blank and zero-quantity rows and preserves the later source index.
- `_iter_content_items` excludes negative quantities.
- `_iter_content_items` rejects non-numeric and non-integer quantities.
- Declared total excludes blank, zero, and negative-quantity rows.
- Declared total remains correct for positive integer quantities.
- All-zero input produces no shippable items and is rejected before M060800 submission.
- Existing automation, editor, UI feedback, and label tests remain green.
