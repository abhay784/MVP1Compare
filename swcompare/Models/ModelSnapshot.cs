namespace SWCompare.Models;

/// <summary>
/// Flat representation of a SOLIDWORKS document at a point in time.
/// Two snapshots (Rev A vs Rev B) are diffed by Comparer to produce ChangeDto list.
/// </summary>
public sealed class ModelSnapshot
{
    public string FilePath { get; init; } = "";
    public string FileKind { get; init; } = "part";   // part | assembly | drawing
    public string? Material { get; set; }
    public Dictionary<string, FeatureInfo> Features { get; init; } = new();
    public Dictionary<string, DimensionInfo> Dimensions { get; init; } = new();
    public MassInfo? Mass { get; set; }
    public List<string> DrawingViewNames { get; init; } = new();
}

public sealed record FeatureInfo
{
    public string Name { get; init; } = "";
    public string TypeName { get; init; } = "";
    public bool IsSuppressed { get; init; }
}

public sealed record DimensionInfo
{
    /// <summary>"D1@Sketch1@Boss-Extrude1.Part" — fully qualified.</summary>
    public string Parameter { get; init; } = "";
    public string FeatureName { get; init; } = "";
    public double ValueMm { get; init; }
    public double? TolLowerMm { get; init; }
    public double? TolUpperMm { get; init; }
    public string[] AffectedGeometry { get; init; } = Array.Empty<string>();
}

public sealed record MassInfo
{
    public double MassKg { get; init; }
    public double VolumeM3 { get; init; }
    public double[]? BoundingBoxM { get; init; }    // [x0,y0,z0,x1,y1,z1]
}
