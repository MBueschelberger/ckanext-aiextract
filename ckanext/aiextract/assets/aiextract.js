ckan.module("aiextract", function (jQuery) {
  return {
    options: {
      parameters: {
        html: {
          contentType: "application/json", // change the content type to text/html
          dataType: "json", // change the data type to html
          dataConverter: function (data) {
            return data;
          },
          language: "json",
        },
      },
    },
    initialize: function () {
      var self = this;
      var p;
      p = this.options.parameters.html;
      console.log("Initialized AIExtract for element: ", this.el);
      var log_length;
      log_length = 0;
      var update = function () {
        jQuery.ajax({
          url: "aiextract/status",
          type: "GET",
          contentType: p.contentType,
          dataType: p.dataType,
          data: { get_param: "value" },
          success: function (data) {
            const haslogs = "logs" in data.status;
            const hasresults = data.status?.data?.files;
            if (hasresults || haslogs) {
              self.el.find('button[name="delete"]').removeClass("invisible");
              self.el.find('div[name="status"]').removeClass("invisible");
            }
            if (!haslogs) return;

            var logs_div = $(self.el).find('ul[name="log"]');
            var existingLogs = new Set(); // Set to track existing logs

            // Collect existing log messages (e.g., by timestamp or unique property)
            logs_div.find("li").each(function () {
              const timestamp = $(this).find(".date").text();
              existingLogs.add(timestamp); // Add timestamp to the Set
            });

            jQuery.each(data.status.logs, function (key, value) {
              if (existingLogs.has(value.timestamp)) {
                // console.log("Skipping duplicate log:", value);
                return; // Skip if log already exists
              }

              logs_div.append(
                "<li class='item " +
                  value.class +
                  "'><i class='fa icon fa-" +
                  value.icon +
                  "'></i><div class='alert alert-" +
                  value.alertlevel +
                  " mb-0 mt-3' role='alert'>" +
                  value.message +
                  "</div><span class='date' title='timestamp'>" +
                  value.timestamp +
                  "</span></li>",
              );
              // console.log("Appending log:", value);
            });

            console.log("AIExtract: status updated");
          },
          error: function (xhr, status, error) {
            console.log("Error:", error);
          },
          complete: function () {
            // call the update function recursively after a delay
            setTimeout(update, 2000);
          },
        });
      };
      update(); // call the update function immediately after initialization
    },
  };
});
