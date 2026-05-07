# SWCompare — SOLIDWORKS extractor service for DrawDiff

A small ASP.NET Core 8 service (Windows-only) that opens two SOLIDWORKS files
via the COM API, walks their feature trees, and returns a JSON changeset
describing every dimension / tolerance / feature / suppression / material
difference. The Python DrawDiff pipeline calls this service over HTTP when
invoked with `--source solidworks`.

## Why a service (not a CLI)

The COM `SldWorks.Application` instance takes seconds to start. A long-lived
service keeps one warm and serves many comparison requests, which keeps
typical-part comparison comfortably under the 30s target.

## Prerequisites

- Windows 10/11 or Windows Server 2019+
- SOLIDWORKS 2020+ installed (the service binds against the active install)
- A valid SOLIDWORKS license available at runtime (Standard / Pro / Premium —
  the API works on all SKUs that ship `sldworks.dll`)
- .NET 8.0 SDK (build) / Runtime (run)

The interop assemblies are loaded from
`C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\api\redist\`. If your install
lives elsewhere (toolbox installs, network shares), edit the `<HintPath>`
entries in `SWCompare.csproj`.

## Build

```powershell
cd swcompare
dotnet restore
dotnet build -c Release
```

## Run (dev / interactive)

```powershell
dotnet run -c Release --urls "http://0.0.0.0:5050"
```

Or after publish:

```powershell
dotnet publish -c Release -r win-x64 --self-contained false
.\bin\Release\net8.0-windows\win-x64\publish\SWCompare.exe --urls "http://0.0.0.0:5050"
```

## Install as a Windows service

```powershell
sc.exe create SWCompare binPath= "C:\path\to\SWCompare.exe --urls http://0.0.0.0:5050" start= auto
sc.exe start SWCompare
```

The service runs under the LocalSystem account by default. If your
SOLIDWORKS license is per-user, use `sc.exe config SWCompare obj= "DOMAIN\user"`
to match the user the license is bound to.

## API

### `GET /health`

```json
{ "status": "ok" }
```

### `POST /compare`

Request:

```json
{
  "old_path": "C:\\drawings\\RevA.SLDPRT",
  "new_path": "C:\\drawings\\RevB.SLDPRT",
  "export_drawing_pdf": true
}
```

Response:

```json
{
  "source": "solidworks",
  "old_file": "C:\\drawings\\RevA.SLDPRT",
  "new_file": "C:\\drawings\\RevB.SLDPRT",
  "file_kind": "part",
  "changes": [
    {
      "type": "dimension",
      "feature_name": "Boss-Extrude1",
      "parameter": "D1@Sketch1",
      "old_value": 20.0,
      "new_value": 25.0,
      "units": "mm",
      "severity": "CRITICAL",
      "confidence": 1.0,
      "rationale": "Δ=5.000mm (25.0% of original)"
    }
  ],
  "exported_pdfs": null,
  "warnings": []
}
```

For drawings (`.SLDDRW`) with `export_drawing_pdf: true`, `exported_pdfs`
will contain base64-encoded PDFs of both sheets so the Python pipeline can
run its existing vision pass on them in addition to the geometry diff.

## License notes

The SOLIDWORKS API is governed by the SOLIDWORKS End User License Agreement.
You must have a valid SOLIDWORKS license on the host running this service.
Distributing the SOLIDWORKS interop DLLs themselves is **not** permitted —
do not commit them to source control. The csproj references them via
`<HintPath>` so they're picked up from the installed location at build time.

## Testing

```powershell
cd swcompare/tests/SWCompare.Tests
dotnet test
```

The test project uses recorded `ModelSnapshot` fixtures (no live COM) so it
runs on any host with .NET 8.

## Configuration

| Setting (env / appsettings) | Default | Effect |
|---|---|---|
| `SW_DIM_THRESHOLD_MM` | `0.1` | Dimension delta below this is MINOR (above → MAJOR; ≥1.0mm or ≥10% → CRITICAL) |
| `Kestrel:Endpoints:Http:Url` | `http://0.0.0.0:5050` | Bind address |

The Python pipeline reads `SOLIDWORKS_SERVICE_URL` from `.env` and POSTs
`/compare`. See the project root README for the Python side.
