using System;

namespace Acme.Mes.Models
{
    public class DrillBreakEvent
    {
        public string MachineId { get; set; }
        public string ToolId { get; set; }
        public DateTime OccurredAt { get; set; }
        public int HoleCountAtBreak { get; set; }
    }
}
