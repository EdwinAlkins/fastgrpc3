### rps

| | small c=1 | small c=16 | medium c=1 | medium c=16 |
|---|---:|---:|---:|---:|
| fastgrpc3 | 4,423 | 4,747 | 4,005 | 4,410 |
| fastgrpc3-deps-1-sync | 3,439 | 3,762 | 3,155 | 3,484 |
| fastgrpc3-deps-1-gen | 2,838 | 3,093 | 2,653 | 2,930 |
| fastgrpc3-deps-3-sync | 2,633 | 2,946 | 2,445 | 2,688 |
| fastgrpc3-deps-3-gen | 1,917 | 2,303 | 1,779 | 2,180 |
| fastgrpc3-deps-10-sync | 1,573 | 1,977 | 1,442 | 1,911 |
| fastgrpc3-deps-10-gen | 973 | 1,481 | 906 | 1,433 |

### p50 ms

| | small c=1 | small c=16 | medium c=1 | medium c=16 |
|---|---:|---:|---:|---:|
| fastgrpc3 | 0.18 | 3.26 | 0.20 | 3.51 |
| fastgrpc3-deps-1-sync | 0.24 | 4.15 | 0.27 | 4.46 |
| fastgrpc3-deps-1-gen | 0.29 | 5.05 | 0.32 | 5.32 |
| fastgrpc3-deps-3-sync | 0.32 | 5.25 | 0.35 | 5.78 |
| fastgrpc3-deps-3-gen | 0.44 | 6.80 | 0.49 | 7.15 |
| fastgrpc3-deps-10-sync | 0.55 | 7.93 | 0.61 | 8.17 |
| fastgrpc3-deps-10-gen | 0.94 | 10.60 | 1.02 | 10.96 |

### p99 ms

| | small c=1 | small c=16 | medium c=1 | medium c=16 |
|---|---:|---:|---:|---:|
| fastgrpc3 | 0.28 | 4.19 | 0.29 | 5.58 |
| fastgrpc3-deps-1-sync | 0.34 | 6.35 | 0.37 | 6.87 |
| fastgrpc3-deps-1-gen | 0.44 | 7.41 | 0.45 | 7.68 |
| fastgrpc3-deps-3-sync | 0.45 | 9.14 | 0.52 | 8.95 |
| fastgrpc3-deps-3-gen | 0.73 | 9.56 | 0.67 | 9.97 |
| fastgrpc3-deps-10-sync | 0.72 | 10.82 | 1.01 | 11.48 |
| fastgrpc3-deps-10-gen | 1.16 | 14.25 | 1.22 | 14.36 |

