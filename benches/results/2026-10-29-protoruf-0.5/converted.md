### µs per message, protoruf 0.5.0 (HEAD) vs 0.4.0 (`effa0e5`), best of 7, two runs each

Eight fields, three nested lines; one model per feature that used to take the converted
(dict) path in Python.

| model | 0.4.0 encode | 0.4.0 decode | 0.5.0 encode | 0.5.0 decode |
|---|---:|---:|---:|---:|
| Plain | 0.53 | 2.93–2.97 | 0.52–0.54 | 2.96–3.14 |
| Renamed (`field(8, proto_name="from")`) | 0.70–0.74 | 3.37–3.38 | 0.52–0.57 | 2.97–3.18 |
| Aliased (`alias="orderNumber"`) | 0.53–0.54 | 3.24–3.28 | 0.52–0.53 | 3.03–3.19 |
| Dated (`datetime.date`) | 0.88–0.90 | 3.71–3.78 | 0.52–0.53 | 3.05–3.29 |
