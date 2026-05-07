using SWCompare.Models;
using SWCompare.Services;

var builder = WebApplication.CreateBuilder(args);

// Singleton — one COM session for the lifetime of the process.
builder.Services.AddSingleton<SolidWorksSession>();
builder.Services.AddSingleton<ModelExtractor>();
builder.Services.AddSingleton<DrawingExporter>();

// Bind threshold from configuration (env var SW_DIM_THRESHOLD_MM or appsettings).
var dimThreshold = builder.Configuration.GetValue<double?>("SW_DIM_THRESHOLD_MM") ?? 0.1;
builder.Services.AddSingleton(new Comparer(dimThreshold));

// Run as a Windows service when launched non-interactively.
builder.Host.UseWindowsService();

var app = builder.Build();

app.MapGet("/health", () => Results.Ok(new { status = "ok" }));

app.MapPost("/compare", (
    CompareRequest req,
    SolidWorksSession session,
    ModelExtractor extractor,
    Comparer comparer,
    DrawingExporter exporter,
    ILogger<Program> log) =>
{
    if (string.IsNullOrWhiteSpace(req.OldPath) || string.IsNullOrWhiteSpace(req.NewPath))
        return Results.BadRequest(new { error = "old_path and new_path are required." });

    if (!File.Exists(req.OldPath)) return Results.BadRequest(new { error = $"old_path not found: {req.OldPath}" });
    if (!File.Exists(req.NewPath)) return Results.BadRequest(new { error = $"new_path not found: {req.NewPath}" });

    log.LogInformation("Comparing {Old} → {New}", req.OldPath, req.NewPath);

    var warnings = new List<string>();
    var oldDoc = session.OpenDocument(req.OldPath, out var oldKind);
    try
    {
        var newDoc = session.OpenDocument(req.NewPath, out var newKind);
        try
        {
            if (oldKind != newKind)
                warnings.Add($"File-kind mismatch: old={oldKind} new={newKind}. Diff may be incoherent.");

            var snapA = extractor.Extract(oldDoc, req.OldPath, oldKind);
            var snapB = extractor.Extract(newDoc, req.NewPath, newKind);
            var changes = comparer.Compare(snapA, snapB);

            ExportedPdfs? pdfs = null;
            if (req.ExportDrawingPdf && oldKind == "drawing")
            {
                try
                {
                    var oldPdf = exporter.ExportToPdfBytes(oldDoc);
                    var newPdf = exporter.ExportToPdfBytes(newDoc);
                    pdfs = new ExportedPdfs
                    {
                        OldB64 = Convert.ToBase64String(oldPdf),
                        NewB64 = Convert.ToBase64String(newPdf),
                    };
                }
                catch (Exception ex)
                {
                    warnings.Add($"Drawing PDF export failed: {ex.Message}");
                }
            }

            return Results.Ok(new CompareResponse
            {
                Source = "solidworks",
                OldFile = req.OldPath,
                NewFile = req.NewPath,
                FileKind = oldKind,
                Changes = changes,
                ExportedPdfs = pdfs,
                Warnings = warnings,
            });
        }
        finally { session.CloseDocument(newDoc); }
    }
    finally { session.CloseDocument(oldDoc); }
});

app.Run();
