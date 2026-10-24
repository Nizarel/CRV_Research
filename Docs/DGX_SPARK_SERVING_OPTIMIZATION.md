---
title: Dual DGX Spark Model Serving Optimization
description: Recommendations for quantization, sparsity, pruning, and serving topology on two NVIDIA DGX Sparks.
ms.date: 2026-10-23
ms.topic: concept
---

## Recommendation

Prioritize quantization and serving-engine tuning. Consider sparsification or
pruning only after benchmarking shows a remaining performance gap.

These recommendations assume primarily large language model (LLM) inference.
They are hardware-informed guidance, not benchmark results from this project's
models. The best configuration depends on model architecture, context length,
concurrency, quality requirements, and serving-runtime support.

## Hardware considerations

Each DGX Spark provides:

* 128 GB of unified memory shared by the CPU and GPU
* 273 GB/s memory bandwidth
* NVIDIA GB10 Grace Blackwell hardware with FP4-capable Tensor Cores
* ConnectX-7 connectivity for linking systems

NVIDIA documents support for models up to 200B parameters on one Spark and 405B
in a dual-Spark configuration. These capacity claims are not guarantees of
interactive latency, concurrency, or support at every precision.

Two Sparks do not form a single transparent 256 GB GPU. Distributed inference
requires a compatible serving runtime and model partitioning, with communication
overhead between machines.

> [!IMPORTANT]
> NVIDIA's advertised 1 PFLOP figure uses FP4 with sparsity. It does not describe
> ordinary dense BF16 inference or guarantee a particular tokens-per-second rate.
> Pruning is not a prerequisite for using the hardware effectively.

See the [NVIDIA DGX Spark hardware specifications][hardware].

## Why quantization is the first optimization

Low-concurrency LLM decoding is often constrained by reading model weights from
memory. Quantization reduces weight storage and memory traffic, potentially
improving serving performance when the runtime provides efficient kernels for
the selected format.

### Approximate weight memory

| Model size | BF16/FP16 | 8-bit  | 4-bit |
|------------|-----------|--------|-------|
| 8B         | 16 GB     | 8 GB   | 4 GB  |
| 32B        | 64 GB     | 32 GB  | 16 GB |
| 70B        | 140 GB    | 70 GB  | 35 GB |
| 120B       | 240 GB    | 120 GB | 60 GB |

These decimal estimates cover weights only. They exclude quantization metadata,
KV cache, activations, runtime buffers, and operating-system memory. Weight
memory is approximately parameter count multiplied by bits per weight, divided
by eight.

For mixture-of-experts (MoE) models, total parameters determine weight storage,
while active parameters affect computation per token. MoE architectures therefore
need separate performance analysis.

### Recommendations by model size

* For 8B-14B models, quantization is optional for capacity but worth benchmarking
  for latency and concurrency.
* For 30B-70B models, quantization is a strong starting point, particularly when
  it allows each model to remain on one Spark.
* For models above 100B parameters, quantization becomes increasingly important.
  Two machines do not eliminate runtime overhead or long-context memory needs.

## Quantization, sparsity, and pruning options

### Weight quantization

Start with a supported 8-bit format as a conservative candidate, then compare
supported 4-bit variants. FP8 and NVFP4 are candidates only where the exact model,
runtime, and GB10 kernels support them efficiently.

Prefer established, compatible quantized checkpoints before performing a custom
conversion. Different 4-bit formats are not interchangeable, and a smaller
checkpoint does not guarantee lower latency.

Validate reasoning, coding, tool use, and domain-specific accuracy before
accepting a quantized model.

### KV-cache quantization

Consider KV-cache quantization when long contexts or simultaneous requests
consume substantial memory. This is separate from weight quantization and needs
its own runtime-support checks and quality evaluation.

### Unstructured pruning

Skip unstructured pruning initially. Setting weights to zero does not
automatically reduce memory use or accelerate dense kernels. Benefits require
compatible sparse storage and execution, and sufficient sparsity to offset
overhead.

### Structured sparsity

Consider structured sparsity, such as 2:4, only with a verified end-to-end
supported path. The model must meet the sparsity pattern, and the serving stack
must use compatible sparse kernels. Recovery training or fine-tuning may be
needed to preserve quality.

### Removing layers, heads, or channels

Treat structural pruning as a model-development project rather than a serving
configuration change. It requires architecture-aware work and quality
validation, and may damage model capability.

## How to use the two Sparks

### Prefer independent serving when the model fits

If a model and its working memory fit comfortably on one Spark, begin with:

* One replica on each Spark, with requests distributed between replicas
* Separate models or pipeline stages, such as generation on one Spark and
  embeddings, reranking, or another model on the other

Independent serving avoids cross-machine communication inside each model's
inference execution. Compare configurations against the actual workload rather
than assuming either arrangement is faster.

### Shard when capacity requires it

Split a model across both Sparks when its weights or required working memory
cannot fit comfortably on one. Benchmark the chosen partitioning strategy,
interconnect configuration, and runtime.

Sharding does not guarantee twice the speed. Its primary benefit may be enabling
a larger model or a larger working set.

## Optimization and validation sequence

1. Select a serving runtime with verified support for the exact model and GB10
   hardware.
2. Establish a quality and performance baseline, using BF16 if it fits.
3. Compare supported 8-bit and 4-bit variants.
4. Tune batching, concurrency, prefix caching, and context limits.
5. Test KV-cache quantization if context length or concurrency is the bottleneck.
6. Consider pruning only if a measured gap remains.

Evaluate at the intended input lengths, output lengths, and concurrency. Record:

* Time to first token
* Inter-token latency
* Aggregate output-token throughput
* Peak memory consumption
* Task quality, including domain-specific accuracy and tool-call correctness

Do not select a configuration using single-request tokens per second alone.
Accept an optimization only when it meets the required quality and latency
thresholds at the intended load.

## Conclusion

For two DGX Sparks, quantization plus serving-engine tuning is the recommended
starting point. A well-supported quantized model running independently on each
Spark is often a better initial configuration than an aggressively pruned model
or a larger model distributed across both.

Pruning remains optional and workload-dependent. Choose it only when supported
execution paths and measured benefits justify the additional development and
quality-recovery effort.

## Source

* [NVIDIA DGX Spark User Guide: Hardware Overview][hardware]

[hardware]: https://docs.nvidia.com/dgx/dgx-spark/hardware.html
