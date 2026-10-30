---
title: Dual DGX Spark Model Serving Optimization
description: Recommendations for local model routing, fast and reasoning models, quantization, and serving topology on one or two NVIDIA DGX Sparks.
ms.date: 2026-10-27
ms.topic: concept
---

## Recommendation

Prioritize quantization and serving-engine tuning. Consider sparsification or
pruning only after benchmarking shows a remaining performance gap.

For interactive workloads, start with a small local System 1 model, a larger
local System 2 model, and a lightweight rule-based router. These components serve
different purposes: a fast model handles routine tasks, while the router selects
the appropriate model or workflow. A dedicated generative router model is not
required initially.

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

## Local System 1, System 2, and routing

### Starting configuration

System 1 means fast responses without extended reasoning. System 2 means
deliberate reasoning for more complex tasks. These are workload roles, not
guarantees associated with a model size.

| Component      | Role                                                       | Starting point                                              |
|----------------|------------------------------------------------------------|-------------------------------------------------------------|
| System 1 model | Extraction, classification, simple Q&A, routine tool choice | A local 4B-8B instruction model                              |
| Local router   | Select a model or workflow                                 | Deterministic rules; add a small classifier only when needed  |
| System 2 model | Complex reasoning, planning, and difficult coding          | A quantized local 24B-32B model; benchmark larger models later |

Disable thinking or extended reasoning for System 1 where the model and serving
runtime support it. Model sizes are starting candidates, not measured winners
or a prescribed checkpoint selection.

```text
Request -> Local router
              |-> Simple or structured task -> System 1
              |-> Complex reasoning task    -> System 2
              |-> Unsupported task          -> Explicit fallback
```

Keep unsupported tasks explicit. A fallback can report that the request is not
supported; an external model should only be used if the deployment allows it.

### Keep routing lightweight

Start with rules based on task type, requested reasoning, context size, and tool
requirements. Do not send every request through an additional generative router
model: it adds latency and competes for the same hardware.

For ambiguous classification, let System 1 assist if evaluation shows that this
improves routing. Add a dedicated learned router only when measured benefits
justify its latency, memory, and maintenance costs.

Escalate using task rules and observable failures, such as invalid structured
output or failed tests, rather than relying only on a model's self-reported
confidence. Define a bounded escalation path so failures do not cause repeated
model calls without an explicit outcome.

### Model residency and shared resources

Keep the small model resident to avoid loading delays. Keep the larger model
resident as well only when the complete working set fits with sufficient
headroom. Budget for both models' weights, KV caches, activations, serving
buffers, and operating-system memory.

Two models that fit in memory still compete for compute and memory bandwidth
when generating simultaneously. Measure mixed workloads, not only isolated
model performance. Large-model capacity is not a guarantee of fast responses.

On one Spark, begin with the 4B-8B fast model and a quantized 24B-32B reasoning
model behind a rule-based router. With two Sparks, benchmark placing the fast
model on one and the reasoning model on the other against the independent
replica and pipeline-stage options below. Do not assume either placement is
faster without measuring the intended workload.

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

### Validate the routing architecture

Compare the two-model configuration with a baseline that sends the same requests
to the reasoning model alone. Use representative simple, complex, ambiguous, and
unsupported requests, including structured-output and tool-use cases.

Record:

* End-to-end p50 and p95 request latency, including routing and escalation
* Task success and domain-specific answer quality
* Routing mistakes, including complex tasks incorrectly sent to System 1
* Escalation frequency and success
* Cold-start or model-loading delays and warm-model latency
* Peak combined memory and performance during simultaneous generation

Set workload-specific quality and latency acceptance thresholds before choosing
the configuration. Retain the router only if its end-to-end benefit justifies
its complexity without unacceptable quality loss. No latency or throughput
targets have been verified on this project's Spark deployment.

## Conclusion

For two DGX Sparks, quantization plus serving-engine tuning is the recommended
starting point. A well-supported quantized model running independently on each
Spark is often a better initial configuration than an aggressively pruned model
or a larger model distributed across both.

Pruning remains optional and workload-dependent. Choose it only when supported
execution paths and measured benefits justify the additional development and
quality-recovery effort.

For fast local interaction, pair a resident 4B-8B System 1 model with a quantized
24B-32B System 2 model and a rule-based router as the initial experiment. Measure
against the reasoning-only baseline before increasing model sizes or introducing
a dedicated learned router.

## Source

* [NVIDIA DGX Spark User Guide: Hardware Overview][hardware]

[hardware]: https://docs.nvidia.com/dgx/dgx-spark/hardware.html
