using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SWCompare.Models;

namespace SWCompare.Services;

/// <summary>
/// Walks a SOLIDWORKS document and produces a flat ModelSnapshot.
///
/// Unit handling: SW returns lengths in meters and angles in radians from
/// IDimension.GetSystemValue3 regardless of the active document units.
/// We convert lengths to mm at extraction time so the diff stage works in
/// drawing-natural units.
/// </summary>
public sealed class ModelExtractor
{
    private const double M_TO_MM = 1000.0;

    public ModelSnapshot Extract(IModelDoc2 doc, string filePath, string fileKind)
    {
        var snap = new ModelSnapshot { FilePath = filePath, FileKind = fileKind };

        // Material — only meaningful on a part.
        if (fileKind == "part" && doc is IPartDoc part)
        {
            snap.Material = part.GetMaterialPropertyName2("", out _);
        }

        // Feature tree
        WalkFeatures(doc, snap);

        // Mass / volume / bbox (parts + assemblies)
        if (fileKind != "drawing")
        {
            try { snap.Mass = ExtractMass(doc); }
            catch { /* mass props can fail on broken models — non-fatal */ }
        }

        // Drawing view names
        if (fileKind == "drawing" && doc is IDrawingDoc drawing)
        {
            CollectDrawingViews(drawing, snap.DrawingViewNames);
        }

        return snap;
    }

    private static void WalkFeatures(IModelDoc2 doc, ModelSnapshot snap)
    {
        var feat = (IFeature?)doc.FirstFeature();
        while (feat != null)
        {
            var info = new FeatureInfo
            {
                Name = feat.Name ?? "",
                TypeName = feat.GetTypeName2() ?? "",
                IsSuppressed = feat.IsSuppressed2(
                    (int)swInConfigurationOpts_e.swThisConfiguration, null) is bool[] arr && arr.Length > 0 && arr[0],
            };
            // Key by Name — SW guarantees uniqueness within a single feature tree.
            snap.Features[info.Name] = info;

            // Dimensions hanging off this feature
            CollectDimensions(feat, snap);

            feat = (IFeature?)feat.GetNextFeature();
        }
    }

    private static void CollectDimensions(IFeature feat, ModelSnapshot snap)
    {
        var disp = (IDisplayDimension?)feat.GetFirstDisplayDimension();
        while (disp != null)
        {
            var dim = (IDimension?)disp.GetDimension2(0);
            if (dim != null)
            {
                var values = (double[]?)dim.GetSystemValue3(
                    (int)swInConfigurationOpts_e.swThisConfiguration, null);
                if (values != null && values.Length > 0)
                {
                    var name = dim.FullName;
                    var (lo, hi) = ReadTolerance(dim);
                    snap.Dimensions[name] = new DimensionInfo
                    {
                        Parameter = name,
                        FeatureName = feat.Name ?? "",
                        ValueMm = values[0] * M_TO_MM,
                        TolLowerMm = lo,
                        TolUpperMm = hi,
                    };
                }
            }
            disp = (IDisplayDimension?)disp.GetNext3();
        }
    }

    private static (double? lower, double? upper) ReadTolerance(IDimension dim)
    {
        try
        {
            var tol = dim.Tolerance;
            if (tol == null) return (null, null);
            // GetValues2 returns nominal/min/max in meters for a length dim
            double minVal = 0, maxVal = 0;
            int type = tol.Type;
            if (type == (int)swTolType_e.swTolBLOCK || type == (int)swTolType_e.swTolNONE)
                return (null, null);
            tol.GetMinValue2((int)swInConfigurationOpts_e.swThisConfiguration, null, out minVal);
            tol.GetMaxValue2((int)swInConfigurationOpts_e.swThisConfiguration, null, out maxVal);
            return (minVal * M_TO_MM, maxVal * M_TO_MM);
        }
        catch
        {
            return (null, null);
        }
    }

    private static MassInfo ExtractMass(IModelDoc2 doc)
    {
        var massProp = (IMassProperty?)doc.Extension.CreateMassProperty()
            ?? throw new InvalidOperationException("CreateMassProperty returned null.");
        return new MassInfo
        {
            MassKg = massProp.Mass,
            VolumeM3 = massProp.Volume,
            BoundingBoxM = (double[]?)doc.Extension.GetBox(
                (int)swBoundingBoxOptions_e.swBoundingBoxIncludeRefPlanes, null),
        };
    }

    private static void CollectDrawingViews(IDrawingDoc drawing, List<string> sink)
    {
        var sheet = (ISheet?)drawing.GetCurrentSheet();
        if (sheet == null) return;
        var view = (IView?)drawing.GetFirstView();
        while (view != null)
        {
            if (!string.IsNullOrEmpty(view.Name)) sink.Add(view.Name);
            view = (IView?)view.GetNextView();
        }
    }
}
