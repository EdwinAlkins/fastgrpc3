### µs per message (lower is better)

| | small | medium | large |
|---|---:|---:|---:|
| bytes | 9.00 | 287 | 23,705 |
| fastgrpc3 encode | 0.12 | 1.05 | 79.98 |
| fastgrpc3 decode | 0.52 | 5.01 | 480 |
| fastgrpc3 decode (no validation) | 0.26 | 2.74 | 266 |
| upb encode (raw) | 0.07 | 0.32 | 19.27 |
| upb decode (raw) | 0.13 | 0.49 | 22.77 |
| upb + Pydantic encode | 4.10 | 57.70 | 4,721 |
| upb + Pydantic decode | 2.37 | 24.14 | 1,984 |
| fastgrpc2 bytes | 9.00 | 288 | 24,000 |
| fastgrpc2 encode | 0.66 | 9.21 | 873 |
| fastgrpc2 decode | 1.44 | 16.13 | 1,696 |

