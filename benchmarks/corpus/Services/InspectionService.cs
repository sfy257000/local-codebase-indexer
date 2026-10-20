using System.Collections.Generic;

namespace Acme.Mes.Services
{
    public class InspectionResult
    {
        public string LotNo { get; set; }
        public string Station { get; set; }
        public bool Passed { get; set; }
        public string DefectCode { get; set; }
    }

    public class InspectionService
    {
        private readonly List<InspectionResult> _results = new List<InspectionResult>();

        public void Save(InspectionResult result)
        {
            _results.Add(result);
        }

        public double PassRate(string station)
        {
            int total = 0, ok = 0;
            foreach (var r in _results)
            {
                if (r.Station != station) continue;
                total++;
                if (r.Passed) ok++;
            }
            return total == 0 ? 0 : (double)ok / total;
        }
    }
}
