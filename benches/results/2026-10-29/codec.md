### µs per message (lower is better)

| | small | medium | large |
|---|---:|---:|---:|
| bytes | 9.00 | 287 | 23,705 |
| fastgrpc3 encode | 0.13 | 1.06 | 76.07 |
| fastgrpc3 decode | 0.47 | 4.98 | 462 |
| fastgrpc3 decode (construct) | 0.23 | 2.65 | 257 |
| upb encode (raw) | 0.07 | 0.31 | 19.12 |
| upb decode (raw) | 0.17 | 0.55 | 22.41 |
| upb + Pydantic encode | 4.24 | 58.36 | 4,852 |
| upb + Pydantic decode | 2.34 | 24.50 | 1,973 |
| fastgrpc2 bytes | 9.00 | 288 | 24,000 |
| fastgrpc2 encode | 0.65 | 9.16 | 849 |
| fastgrpc2 decode | 1.46 | 16.23 | 1,761 |

