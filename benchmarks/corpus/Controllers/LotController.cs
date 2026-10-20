using System.Web.Http;
using Acme.Mes.Services;

namespace Acme.Mes.Controllers
{
    [RoutePrefix("api/lot")]
    public class LotController : ApiController
    {
        private readonly LotService _lotService;

        public LotController(LotService lotService)
        {
            _lotService = lotService;
        }

        [HttpPost, Route("{lotNo}/finish")]
        public IHttpActionResult Finish(string lotNo)
        {
            _lotService.FinishProduction(lotNo);
            return Ok();
        }
    }
}
