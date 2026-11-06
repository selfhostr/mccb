# UI messages

`{locale}.json` holds every visible string, `en` is the base. Paraglide compiles them into `m.<key>()`.

- Key: `<zone>_<component>_<meaning>`, snake_case, for example `migrations_bar_selected`.
- One key is one whole sentence with its `{variables}`. Never concatenate fragments, never put markup in a message.
- Counts are written so they read in every language without a plural form ("Selected: {count}").
- Text that comes from the server (API errors, test messages, connector names) stays as served.
- Product and protocol names, commands and file names are not translated.
- No em dash, no emoji.

`npm run i18n:check` fails when a locale misses a key or has an unknown one, changes a `{variable}`, when the source
uses an unknown key or when a key is no longer used.
