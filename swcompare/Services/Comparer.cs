using SWCompare.Models;

namespace SWCompare.Services;

/// <summary>
/// Diffs two ModelSnapshots and emits a flat list of ChangeDto.
///
/// Severity / confidence / rationale are populated here using deterministic
/// rules. The Python pipeline can override via SW_RECLASSIFY=true; otherwise
/// it trusts what we emit. Mirrors `pipeline/solidworks_severity.py`.
/// </summary>
public sealed class Comparer
{
    private static readonly string[] StructuralKeywords =
        { "Boss", "Cut", "Hole", "Pattern", "Fillet", "Chamfer", "Shell",
          "Rib", "Loft", "Sweep", "Revolve", "Extrude", "Mirror" };
    private static readonly string[] CosmeticKeywords =
        { "Sketch", "Reference", "Plane", "Axis", "Point", "CosmeticThread",
          "Annotation", "DimXpert" };

    private readonly double _dimThresholdMm;

    public Comparer(double dimensionThresholdMm = 0.1)
    {
        _dimThresholdMm = dimensionThresholdMm;
    }

    public List<ChangeDto> Compare(ModelSnapshot a, ModelSnapshot b)
    {
        var changes = new List<ChangeDto>();

        if (!string.Equals(a.Material, b.Material, StringComparison.Ordinal))
        {
            changes.Add(new ChangeDto
            {
                Type = "material",
                Parameter = "material",
                OldValue = a.Material,
                NewValue = b.Material,
                Severity = "CRITICAL",
                Confidence = 1.0,
                Rationale = $"Material changed: {a.Material} → {b.Material}",
            });
        }

        // Features — added / removed / suppression
        foreach (var (name, fa) in a.Features)
        {
            if (!b.Features.TryGetValue(name, out var fb))
            {
                changes.Add(FeatureRemoved(fa));
            }
            else if (fa.IsSuppressed != fb.IsSuppressed)
            {
                changes.Add(SuppressionToggled(fa, fb));
            }
        }
        foreach (var (name, fb) in b.Features)
        {
            if (!a.Features.ContainsKey(name))
                changes.Add(FeatureAdded(fb));
        }

        // Dimensions + tolerances
        foreach (var (name, da) in a.Dimensions)
        {
            if (!b.Dimensions.TryGetValue(name, out var db))
            {
                changes.Add(DimensionRemoved(da));
                continue;
            }
            if (Math.Abs(da.ValueMm - db.ValueMm) > 1e-9)
                changes.Add(DimensionDelta(da, db));
            if (TolerancesDiffer(da, db))
                changes.Add(ToleranceDelta(da, db));
        }
        foreach (var (name, db) in b.Dimensions)
        {
            if (!a.Dimensions.ContainsKey(name))
                changes.Add(DimensionAdded(db));
        }

        // Mass / volume / bbox — informational only
        if (a.Mass != null && b.Mass != null)
        {
            if (Math.Abs(a.Mass.MassKg - b.Mass.MassKg) > 1e-6)
                changes.Add(MassChange("mass", a.Mass.MassKg, b.Mass.MassKg, "kg"));
            if (Math.Abs(a.Mass.VolumeM3 - b.Mass.VolumeM3) > 1e-9)
                changes.Add(MassChange("volume", a.Mass.VolumeM3, b.Mass.VolumeM3, "m^3"));
        }

        return changes;
    }

    // -- Severity helpers ----------------------------------------------------

    private static bool IsStructural(string featureName)
    {
        if (CosmeticKeywords.Any(k => featureName.Contains(k, StringComparison.OrdinalIgnoreCase)))
            return false;
        if (StructuralKeywords.Any(k => featureName.Contains(k, StringComparison.OrdinalIgnoreCase)))
            return true;
        return true; // unknown → assume structural (safer)
    }

    private static ChangeDto FeatureAdded(FeatureInfo f) => new()
    {
        Type = "feature_added",
        FeatureName = f.Name,
        NewValue = f.TypeName,
        Severity = IsStructural(f.Name) ? "CRITICAL" : "MINOR",
        Confidence = 1.0,
        Rationale = $"{(IsStructural(f.Name) ? "Structural" : "Cosmetic")} feature added: {f.Name}",
    };

    private static ChangeDto FeatureRemoved(FeatureInfo f) => new()
    {
        Type = "feature_removed",
        FeatureName = f.Name,
        OldValue = f.TypeName,
        Severity = IsStructural(f.Name) ? "CRITICAL" : "MINOR",
        Confidence = 1.0,
        Rationale = $"{(IsStructural(f.Name) ? "Structural" : "Cosmetic")} feature removed: {f.Name}",
    };

    private static ChangeDto SuppressionToggled(FeatureInfo a, FeatureInfo b) => new()
    {
        Type = "suppression",
        FeatureName = a.Name,
        Parameter = "IsSuppressed",
        OldValue = a.IsSuppressed,
        NewValue = b.IsSuppressed,
        Severity = IsStructural(a.Name) ? "MAJOR" : "MINOR",
        Confidence = 1.0,
        Rationale = $"Suppression toggled on {a.Name}: {a.IsSuppressed} → {b.IsSuppressed}",
    };

    private ChangeDto DimensionDelta(DimensionInfo a, DimensionInfo b)
    {
        var delta = Math.Abs(b.ValueMm - a.ValueMm);
        var pct = Math.Abs(a.ValueMm) > 0 ? (delta / Math.Abs(a.ValueMm)) * 100 : double.PositiveInfinity;
        string sev;
        if (delta >= 1.0 || pct >= 10.0) sev = "CRITICAL";
        else if (delta >= _dimThresholdMm) sev = "MAJOR";
        else sev = "MINOR";
        return new ChangeDto
        {
            Type = "dimension",
            FeatureName = a.FeatureName,
            Parameter = a.Parameter,
            OldValue = a.ValueMm,
            NewValue = b.ValueMm,
            Units = "mm",
            AffectedGeometry = a.AffectedGeometry,
            Severity = sev,
            Confidence = 1.0,
            Rationale = $"Δ={delta:F3}mm ({pct:F1}% of original)",
        };
    }

    private static ChangeDto DimensionAdded(DimensionInfo d) => new()
    {
        Type = "dimension",
        FeatureName = d.FeatureName,
        Parameter = d.Parameter,
        OldValue = null,
        NewValue = d.ValueMm,
        Units = "mm",
        Severity = "MAJOR",
        Confidence = 1.0,
        Rationale = "Dimension added",
    };

    private static ChangeDto DimensionRemoved(DimensionInfo d) => new()
    {
        Type = "dimension",
        FeatureName = d.FeatureName,
        Parameter = d.Parameter,
        OldValue = d.ValueMm,
        NewValue = null,
        Units = "mm",
        Severity = "MAJOR",
        Confidence = 1.0,
        Rationale = "Dimension removed",
    };

    private static bool TolerancesDiffer(DimensionInfo a, DimensionInfo b)
    {
        return !Nullable.Equals(a.TolLowerMm, b.TolLowerMm)
            || !Nullable.Equals(a.TolUpperMm, b.TolUpperMm);
    }

    private static ChangeDto ToleranceDelta(DimensionInfo a, DimensionInfo b)
    {
        var oldBand = (a.TolLowerMm ?? 0, a.TolUpperMm ?? 0);
        var newBand = (b.TolLowerMm ?? 0, b.TolUpperMm ?? 0);
        var tightened = newBand.Item1 > oldBand.Item1 || newBand.Item2 < oldBand.Item2;
        return new ChangeDto
        {
            Type = "tolerance",
            FeatureName = a.FeatureName,
            Parameter = a.Parameter,
            Nominal = a.ValueMm,
            ToleranceOld = a.TolLowerMm.HasValue ? new[] { a.TolLowerMm.Value, a.TolUpperMm ?? 0 } : null,
            ToleranceNew = b.TolLowerMm.HasValue ? new[] { b.TolLowerMm.Value, b.TolUpperMm ?? 0 } : null,
            Severity = tightened ? "CRITICAL" : "MAJOR",
            Confidence = 1.0,
            Rationale = tightened ? "Tolerance tightened" : "Tolerance shifted",
        };
    }

    private static ChangeDto MassChange(string kind, double oldVal, double newVal, string units) => new()
    {
        Type = kind,
        Parameter = kind,
        OldValue = oldVal,
        NewValue = newVal,
        Units = units,
        Severity = "MINOR",
        Confidence = 1.0,
        Rationale = $"{kind} delta (informational)",
    };
}
