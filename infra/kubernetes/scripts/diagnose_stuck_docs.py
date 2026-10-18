"""Diagnose why specific instrument-diagram PDFs are resource-intensive: run Document
Intelligence Layout extraction directly (bypassing the queue/worker path) and report page
count, figure count, elapsed time, and markdown length — without caching figures, so a
figure-count blowup shows up as a slow/large result rather than a cache-write crash."""

from __future__ import annotations

import asyncio
import json
import time


async def main() -> None:
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
    from azure.ai.documentintelligence.models import DocumentContentFormat
    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.blob.aio import BlobServiceClient

    from ingestion.document_intelligence import create_analyze_request

    blobs = [
        "10-Installation électrique & Instrumentation/18-Instruments/Flow instrument/SP0904-I705.00-G05-0008 Sub 01 Status A.pdf",
        "10-Installation électrique & Instrumentation/18-Instruments/Flow instrument/SP0904-I705.00-I30-0016 Sub 01 Status A.pdf",
        "10-Installation électrique & Instrumentation/18-Instruments/Flow instrument/SP0904-I705.00-I06-0015 Sub 01 Status A.pdf",
        "10-Installation électrique & Instrumentation/18-Instruments/Temperature Transmitters/SP0904-I707.00-I30-0014 Sub 02 Status A.pdf",
        "10-Installation électrique & Instrumentation/18-Instruments/Flow instrument/SP0904-I705.00-Q15-0012 Sub 01 Status A.pdf",
    ]

    credential = DefaultAzureCredential()
    blob_service = BlobServiceClient(
        "https://stnyuwyhzhjriuu.blob.core.windows.net", credential=credential
    )
    di_client = DocumentIntelligenceClient(
        "https://cog-di-nyuwyhzhjriuu.cognitiveservices.azure.com/", credential=credential
    )

    results = []
    try:
        container = blob_service.get_container_client("content")
        for blob_name in blobs:
            print(f"[start] {blob_name}", flush=True)
            downloader = await container.get_blob_client(blob_name).download_blob()
            source_bytes = await downloader.readall()
            size_bytes = len(source_bytes)
            print(f"[downloaded] {size_bytes} bytes", flush=True)

            started = time.perf_counter()
            poller = await di_client.begin_analyze_document(
                "prebuilt-layout",
                create_analyze_request(source_bytes),
                output_content_format=DocumentContentFormat.MARKDOWN,
                output=["figures"],
            )
            print(f"[poller started] operation_id={poller.details.get('operation_id')}", flush=True)
            result = await poller.result()
            elapsed = time.perf_counter() - started
            print(f"[poller finished] elapsed={elapsed:.1f}s", flush=True)

            figures = getattr(result, "figures", None) or []
            pages = getattr(result, "pages", None) or []
            markdown = getattr(result, "content", "") or ""
            results.append(
                {
                    "blob_name": blob_name,
                    "size_bytes": size_bytes,
                    "page_count": len(pages),
                    "figure_count": len(figures),
                    "markdown_length": len(markdown),
                    "elapsed_seconds": round(elapsed, 1),
                }
            )
            print(json.dumps(results[-1], ensure_ascii=False))
    finally:
        await di_client.close()
        await blob_service.close()
        await credential.close()

    print("---SUMMARY---")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
