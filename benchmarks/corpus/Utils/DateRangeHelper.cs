using System;

namespace Acme.Mes.Utils
{
    public static class DateRangeHelper
    {
        // 滾動日期區間：回傳今天往前 N 天的起迄時間
        public static (DateTime from, DateTime to) LastDays(int days)
        {
            var to = DateTime.Today.AddDays(1);
            return (to.AddDays(-days - 1), to);
        }
    }
}
