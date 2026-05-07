using SWCompare.Models;
using SWCompare.Services;
using Xunit;

namespace SWCompare.Tests;

public class ComparerTests
{
    private static ModelSnapshot Snap(string mat,
        Dictionary<string, FeatureInfo>? feats = null,
        Dictionary<string, DimensionInfo>? dims = null) =>
        new()
        {
            FilePath = "test.sldprt",
            FileKind = "part",
            Material = mat,
            Features = feats ?? new(),
            Dimensions = dims ?? new(),
        };

    [Fact]
    public void MaterialChange_IsCritical()
    {
        var a = Snap("Steel");
        var b = Snap("Aluminum");
        var changes = new Comparer().Compare(a, b);
        var mat = Assert.Single(changes, c => c.Type == "material");
        Assert.Equal("CRITICAL", mat.Severity);
        Assert.Equal(1.0, mat.Confidence);
    }

    [Fact]
    public void LargeDimensionDelta_IsCritical()
    {
        var dim = new DimensionInfo { Parameter = "D1@Sketch1", FeatureName = "Boss-Extrude1", ValueMm = 20 };
        var dim2 = dim with { ValueMm = 25 };
        var changes = new Comparer().Compare(
            Snap("Steel", dims: new() { ["D1@Sketch1"] = dim }),
            Snap("Steel", dims: new() { ["D1@Sketch1"] = dim2 }));
        var d = Assert.Single(changes, c => c.Type == "dimension");
        Assert.Equal("CRITICAL", d.Severity);
    }

    [Fact]
    public void SubThresholdDimensionDelta_IsMinor()
    {
        var a = new DimensionInfo { Parameter = "D2", FeatureName = "X", ValueMm = 10.0 };
        var b = a with { ValueMm = 10.05 };
        var changes = new Comparer().Compare(
            Snap("Steel", dims: new() { ["D2"] = a }),
            Snap("Steel", dims: new() { ["D2"] = b }));
        var d = Assert.Single(changes, c => c.Type == "dimension");
        Assert.Equal("MINOR", d.Severity);
    }

    [Fact]
    public void StructuralFeatureAdded_IsCritical()
    {
        var changes = new Comparer().Compare(
            Snap("Steel"),
            Snap("Steel", feats: new()
            {
                ["Boss-Extrude5"] = new FeatureInfo { Name = "Boss-Extrude5", TypeName = "Extrude" },
            }));
        var add = Assert.Single(changes, c => c.Type == "feature_added");
        Assert.Equal("CRITICAL", add.Severity);
    }

    [Fact]
    public void ToleranceTightened_IsCritical()
    {
        var a = new DimensionInfo
        { Parameter = "D1", FeatureName = "X", ValueMm = 10, TolLowerMm = -0.1, TolUpperMm = 0.1 };
        var b = a with { TolLowerMm = -0.05, TolUpperMm = 0.05 };
        var changes = new Comparer().Compare(
            Snap("Steel", dims: new() { ["D1"] = a }),
            Snap("Steel", dims: new() { ["D1"] = b }));
        var tol = Assert.Single(changes, c => c.Type == "tolerance");
        Assert.Equal("CRITICAL", tol.Severity);
    }
}
