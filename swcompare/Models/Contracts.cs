using System.Text.Json.Serialization;

namespace SWCompare.Models;

public sealed record CompareRequest
{
    [JsonPropertyName("old_path")]      public string OldPath { get; init; } = "";
    [JsonPropertyName("new_path")]      public string NewPath { get; init; } = "";
    [JsonPropertyName("export_drawing_pdf")] public bool ExportDrawingPdf { get; init; } = true;
}

public sealed record ChangeDto
{
    [JsonPropertyName("type")]              public string Type { get; init; } = "";
    [JsonPropertyName("feature_name")]      public string? FeatureName { get; init; }
    [JsonPropertyName("parameter")]         public string? Parameter { get; init; }
    [JsonPropertyName("old_value")]         public object? OldValue { get; init; }
    [JsonPropertyName("new_value")]         public object? NewValue { get; init; }
    [JsonPropertyName("units")]             public string? Units { get; init; }
    [JsonPropertyName("nominal")]           public double? Nominal { get; init; }
    [JsonPropertyName("tolerance_old")]     public double[]? ToleranceOld { get; init; }
    [JsonPropertyName("tolerance_new")]     public double[]? ToleranceNew { get; init; }
    [JsonPropertyName("affected_geometry")] public string[] AffectedGeometry { get; init; } = Array.Empty<string>();
    [JsonPropertyName("drawing_views")]     public string[] DrawingViews { get; init; } = Array.Empty<string>();
    [JsonPropertyName("severity")]          public string? Severity { get; init; }
    [JsonPropertyName("confidence")]        public double? Confidence { get; init; }
    [JsonPropertyName("rationale")]         public string? Rationale { get; init; }
}

public sealed record ExportedPdfs
{
    [JsonPropertyName("old_b64")] public string OldB64 { get; init; } = "";
    [JsonPropertyName("new_b64")] public string NewB64 { get; init; } = "";
}

public sealed record CompareResponse
{
    [JsonPropertyName("source")]        public string Source { get; init; } = "solidworks";
    [JsonPropertyName("old_file")]      public string OldFile { get; init; } = "";
    [JsonPropertyName("new_file")]      public string NewFile { get; init; } = "";
    [JsonPropertyName("file_kind")]     public string FileKind { get; init; } = "part";
    [JsonPropertyName("changes")]       public List<ChangeDto> Changes { get; init; } = new();
    [JsonPropertyName("exported_pdfs")] public ExportedPdfs? ExportedPdfs { get; init; }
    [JsonPropertyName("warnings")]      public List<string> Warnings { get; init; } = new();
}
