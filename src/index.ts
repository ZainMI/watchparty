export default {
  async fetch(request: Request): Promise<Response> {
    return new Response("OK - worker deployed");
  },
};
