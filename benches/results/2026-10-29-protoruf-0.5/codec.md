### µs per message (lower is better)

| | small | medium | large |
|---|---:|---:|---:|
| bytes | 9.00 | 287 | 23,705 |
| fastgrpc3 encode | 0.12 | 1.05 | 77.89 |
| fastgrpc3 decode | 0.52 | 5.06 | 467 |
| fastgrpc3 decode (construct) | 0.21 | 2.65 | 254 |
| upb encode (raw) | 0.06 | 0.31 | 19.20 |
| upb decode (raw) | 0.13 | 0.47 | 23.17 |
| upb + Pydantic encode | 4.16 | 58.34 | 4,706 |
| upb + Pydantic decode | 2.37 | 23.47 | 1,956 |
| fastgrpc2 bytes | 9.00 | 288 | 24,000 |
| fastgrpc2 encode | 0.65 | 8.82 | 864 |
| fastgrpc2 decode | 1.45 | 16.44 | 1,708 |

