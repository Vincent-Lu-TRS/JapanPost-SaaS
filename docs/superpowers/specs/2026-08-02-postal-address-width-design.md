# Japan Post Address Width Design

## Goal

Prevent recipient submission failures when Japan Post counts non-ASCII characters as wider than Python's ordinary character count. The confirmed case is order `imy2038230`, whose Address 2 has 76 Unicode characters but 82 Japan Post width units.

## Confirmed Failure

The source address is:

```text
Aleea Locotenent Gheorghe Stâlpeanu 11‚ bl 8‚ sc B‚ et 4‚ ap 38‚ interfon 38
```

The current splitter checks `len(text) <= 80`, so it submits the entire address as `addrToBean.add2`. Japan Post treats `â` and each `‚` as two units and rejects the field with `Input is too long`. The request remains on M060505 and automation correctly stops without producing a label.

The later `extra_streamlit_components/CookieManager` directory read error is unrelated to the order failure and is outside this change.

## Design

Add a shared Japan Post text preparation boundary before address splitting:

1. Normalize Latin letters with diacritics to their base ASCII letters, for example `â` to `a`.
2. Map Unicode punctuation used as separators or quotation marks to safe ASCII equivalents, for example `‚` to `,`.
3. Preserve non-Latin scripts instead of deleting them.
4. Measure field width as one unit for ASCII characters and two units for non-ASCII characters.
5. Split on word boundaries where possible so Address 1 and Address 2 each remain within 80 units and Address 3 remains within 36 units.

For the confirmed address, preparation produces:

```text
Aleea Locotenent Gheorghe Stalpeanu 11, bl 8, sc B, et 4, ap 38, interfon 38
```

This is 76 ASCII units and can be submitted in Address 2 without losing address information.

## Scope

- Apply normalization and weighted splitting to street and city values used for `addrToBean.add1`, `addrToBean.add2`, and `addrToBean.add3`.
- Keep recipient ID handling unchanged: PCCC and PRC ID remain isolated in Address 3 where required.
- Do not modify source Google Sheets values or Streamlit editor values.
- Do not change recipient name, state, postal code, phone, content item, or shipping-method logic in this fix.
- Do not change the unrelated CookieManager dependency in this fix.

## Error Handling

- Never silently drop overflow while splitting a normal address. If prepared text cannot fit within the available address fields, raise a descriptive error before submitting M060505.
- Include both ordinary character count and Japan Post width units in diagnostics so future failures can be distinguished quickly.

## Tests

- Reproduce `imy2038230` and prove its prepared Address 2 is ASCII-safe and at most 80 width units.
- Prove Unicode punctuation is mapped without deleting separators.
- Prove Latin diacritics are transliterated while non-Latin text is preserved.
- Prove all address fields obey their weighted limits.
- Preserve existing Korean PCCC and ordinary English-address behavior.
- Run the complete test suite and Python compilation checks before publishing the feature branch.
